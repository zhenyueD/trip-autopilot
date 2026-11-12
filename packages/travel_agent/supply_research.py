"""One paid open-ended supply search plus at most six original-page reads."""
from __future__ import annotations

import copy
import hashlib
import json
import time

from . import material_reader, public_search
from .comparison_card_budget import now, private_json
from .comparison_card_research import ComparisonResearch, INDEPENDENCE_CAVEAT, PAGE_READ_SECONDS
from .deepseek_discovery import DeepSeekSearchResponse, discovery_urls
from .supply_evidence import (SOCIAL_TYPES, evidence_fields, group_independence,
                              is_reddit_post, source_kind, lead_only_reason)
from .supply_read_order import schedule_reads
from .supply_discovery_queue import discovery_queue, queue_read_receipts

MAX_PAGE_READS = 6
ATTRIBUTION_SECONDS = 12
SAFE_READ_FIELDS = ('status', 'reason', 'http_status', 'fetched_at', 'body_sha256', 'truncated',
                    'method', 'redirect_chain', 'verification', 'source_provenance',
                    'timeout_stage', 'request_delivery_status', 'request_delivery_scope',
                    'error_type', 'error_code', 'usage_status', 'usage_complete',
                    'provider_request_id', 'provider_request_id_present', 'bytes_read')


def _scope(seed):
    if not isinstance(seed, dict):
        raise ValueError('Supply seed must be an object')
    for key in ('id', 'decision_need', 'destination'):
        if not isinstance(seed.get(key), str) or not seed[key].strip():
            raise ValueError('Supply seed requires ' + key)
    scope = {key: copy.deepcopy(seed[key]) for key in
             ('id', 'decision_need', 'destination', 'traveler_profile', 'dates', 'timezone',
              'conditions', 'languages', 'scenario_kind') if key in seed}
    if len(json.dumps(scope, ensure_ascii=False)) > 16000:
        raise ValueError('Supply research scope is too large')
    return scope


def _query(scope):
    return ('Discover concrete travel supplies for the exact decision need below. '
            'There is no preset candidate whitelist; preserve traveler profile, destination, dates, '
            'timezone and conditions. Find official/operator original pages for hard facts such as '
            'current prices, opening and availability; the responsible authority for permissions; '
            'Maps/Routes for transport under matching endpoints, mode and time. Also explicitly '
            'find recent first-hand experience: social posts within 7 days and reviews within '
            '30 days (a strict bound for one month). Include an exact public Reddit post URL among '
            'the direct URLs for an actual original-page access attempt. Search answers and their '
            'dates are leads only. Keep publication, experience and retrieval dates separate. '
            'Unknown, future or outdated dates cannot establish current experience. Never classify '
            'a publisher as official solely from its name/domain. Translations, reposts and shared '
            'upstream sources are not independent. Stop at login/app/challenge walls, with no '
            'API/RSS or alternate-host bypass. Unknown evidence stays unknown and one official '
            'source remains 未交叉确认; do not promise future seats/crowding or exhaustive absence. '
            'Request: ' + json.dumps(scope, ensure_ascii=False))


def _candidate_urls(response, deadline):
    if isinstance(response, DeepSeekSearchResponse):
        return discovery_urls(response)
    # Existing extraction validates all URLs. Direct URLs get their own pass so
    # six grounding links cannot hide a returned public Reddit original.
    direct_response = copy.deepcopy(response)
    for candidate in direct_response.get('candidates', []) or []:
        if isinstance(candidate, dict):
            candidate.pop('groundingMetadata', None)
    direct = public_search._result_urls(direct_response, deadline=deadline)
    grounded = public_search._result_urls(response, deadline=deadline)
    urls = list(dict.fromkeys([*direct, *grounded]))
    return urls


def _retained_document(document, url):
    if not isinstance(document, dict):
        return {'url': url, 'status': 'unavailable', 'reason': 'invalid_reader_result', 'text': ''}
    result = copy.deepcopy(document)
    kind, _ = source_kind(result, result.get('url') or url)
    text = result.get('text')
    reason = lead_only_reason(result, text)
    if reason:
        result.update(lead_only=True, lead_only_reason=reason)
    if kind in SOCIAL_TYPES and isinstance(text, str) and text:
        provenance = result.get('source_provenance')
        provenance = provenance if isinstance(provenance, dict) else {}
        already_excerpt = (result.get('content_complete') is False
                           and provenance.get('excerpt') == text and len(text) <= 200)
        if not already_excerpt:
            text = text[:min(200, len(text) // 2)]
        result.update(text=text, content_complete=False, truncated=True,
                      factual_status='social_unverified')
        result['source_provenance'] = {**provenance, 'excerpt': text}
    return result


def _capturing_reader(reader, captured):
    def read(url, *, deadline):
        try:
            document = reader(url, deadline=deadline)
        except Exception as error:
            usage = getattr(error, 'usage', {})
            facts = {key: usage[key] for key in SAFE_READ_FIELDS if isinstance(usage, dict) and key in usage}
            document = {'url': url, 'status': 'unavailable', 'reason': 'reader_error', 'text': '',
                        'fetched_at': now(), 'error_type': type(error).__name__, **facts}
        document = _retained_document(document, url)
        captured['document'] = document
        return document
    return read


def _source_id(url):
    return 'supply-source-' + hashlib.sha256(url.encode()).hexdigest()[:16]


def _collect_read(transport, captured, url, index, artifacts, deadline):
    source, receipt = transport._read(url, index, artifacts, deadline)
    document = captured['document']
    receipt.update({key: copy.deepcopy(document[key]) for key in SAFE_READ_FIELDS if key in document})
    source_id = _source_id(url)
    evidence = evidence_fields(document, source_id=source_id, url=document.get('url') or url)
    ledger = {**evidence, **receipt, 'requested_url': url, 'reader_invoked': True}
    if source is not None:
        # ComparisonResearch may truncate further than the underlying reader.
        evidence['content_complete'] = source['content_complete']
        source.update(copy.deepcopy(evidence), id=source_id, original_read_status='read')
        if evidence['source_type'] in SOCIAL_TYPES:
            source['factual_status'] = 'social_unverified'
    return source, receipt, ledger


def _reddit_summary(ledger):
    rows = [row for row in ledger if is_reddit_post(row['requested_url'])]
    first = next((row for row in rows if row['reader_invoked']), rows[0] if rows else None)
    return {'discovery_requested': True, 'discovered': bool(rows),
            'reader_invoked': bool(first and first['reader_invoked']),
            'url': first['requested_url'] if first else None,
            'read_status': first['read_status'] if first else 'not_attempted',
            'reason': first['reason'] if first else 'no_public_post_url_returned',
            'http_status': first.get('http_status') if first else None,
            'request_delivery_status': first.get('request_delivery_status') if first else None}


def _discovery_admissions(response, schedule):
    if not isinstance(response, DeepSeekSearchResponse):
        return None
    _, admissions = discovery_queue(response)
    for trace in schedule:
        matches = [row for row in admissions if row['requested_url'] == trace['requested_url']]
        trace['discovery_occurrence_ids'] = [row['discovery_id'] for row in matches]
        trace['discovery_origin'] = next(row['origin'] for row in matches
                                         if row['admission_status'] == 'queued')
    return admissions


def _save_ledgers(artifacts, ledger, admissions):
    private_json(artifacts / 'source-ledger.json', ledger)
    if admissions is not None:
        private_json(artifacts / 'discovery-ledger.json', queue_read_receipts(admissions, ledger))


class SupplyResearch:
    """Shares the caller's paid ledger; webpage count is a separate allowance."""

    def __init__(self, budget, *, searcher=None, reader=None, max_page_reads=MAX_PAGE_READS):
        if type(max_page_reads) is not int or not 1 <= max_page_reads <= MAX_PAGE_READS:
            raise ValueError('Page read allowance must be between 1 and 6')
        self.budget, self.max_page_reads = budget, max_page_reads
        self.searcher = searcher or public_search._ground
        self.reader = reader or material_reader.read_public_link

    def __call__(self, seed, *, request_id):
        scope, captured = _scope(seed), {}
        artifacts = self.budget.directory / 'supply-research-artifacts' / hashlib.sha256(str(request_id).encode()).hexdigest()
        transport = ComparisonResearch(self.budget, searcher=self.searcher,
                                       reader=_capturing_reader(self.reader, captured))
        query = _query(scope)
        response, receipt = transport._search(query, scope, request_id, artifacts)
        attribution_started = time.monotonic()
        candidates = _candidate_urls(response, attribution_started + ATTRIBUTION_SECONDS)
        schedule = schedule_reads(response, candidates)
        admissions = _discovery_admissions(response, schedule)
        candidates = [row['requested_url'] for row in schedule]
        attribution_ms = round((time.monotonic() - attribution_started) * 1000)
        deadline = time.monotonic() + PAGE_READ_SECONDS
        sources, reads, ledger = [], [], []
        for index, trace in enumerate(schedule):
            url = trace['requested_url']
            reason = 'scheduled_not_attempted' if index < self.max_page_reads else 'page_read_limit'
            row = evidence_fields({'status': 'not_attempted', 'reason': reason},
                                  source_id=_source_id(url), url=url)
            ledger.append({**row, **trace, 'reader_invoked': False})
        _save_ledgers(artifacts, ledger, admissions)
        for index, url in enumerate(candidates[:self.max_page_reads]):
            source, read, row = _collect_read(transport, captured, url, index, artifacts, deadline)
            reads.append(read)
            ledger[index] = {**row, **schedule[index], 'actual_read_order': index + 1}
            if source is not None:
                sources.append(source)
            _save_ledgers(artifacts, ledger, admissions)
        group_independence(sources, ledger)
        context = {'query': query, 'scope': scope, 'retrieved_at': now(), 'search_receipt': receipt,
                   'original_reads': reads, 'skipped_candidates': candidates[self.max_page_reads:],
                   'page_read_budget': {'limit': self.max_page_reads, 'attempted': len(reads),
                                        'remaining': self.max_page_reads - len(reads),
                                        'seconds_limit': PAGE_READ_SECONDS,
                                        'unit': 'original_page_reader_invocations', 'paid_call_budget': False},
                   'attribution_resolution_budget': {'seconds_limit': ATTRIBUTION_SECONDS,
                                                     'elapsed_ms': attribution_ms},
                   'read_order_policy': {'buckets': ['likely_official_or_first_party',
                                                    'likely_authority', 'other'],
                                         'basis': 'untrusted_discovery_hints_only',
                                         'source_authentication': False},
                   'reddit': _reddit_summary(ledger), 'independence_caveat': INDEPENDENCE_CAVEAT,
                   'freshness_policy': {'social_days': 7, 'review_days': 30,
                                        'review_window': 'strict_30_days', 'clock': 'reader_fetched_at'},
                   'exhaustive': False, 'search_text_used_as_evidence': False,
                   'artifact_directory': str(artifacts)}
        if admissions is not None:
            context.update(discovery_queue_ledger_path=str(artifacts / 'discovery-ledger.json'),
                           discovery_queue_counts={
                               'top_level_results': len(response.urls),
                               'summary_url_occurrences': len(response.summary_leads),
                               **{status: sum(row['admission_status'] == status for row in admissions)
                                  for status in ('queued', 'duplicate', 'rejected')}})
        enriched = {**copy.deepcopy(seed), 'sources': sources, 'research_context': context,
                    'source_ledger': ledger}
        _save_ledgers(artifacts, ledger, admissions)
        private_json(artifacts / 'enriched-seed.json', enriched)
        return enriched
