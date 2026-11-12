"""Compose existing research with typed Places and a real browser tool bridge."""
from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlsplit, urlunsplit

from .comparison_card_budget import now, private_json
from .deepseek_discovery import DeepSeekDiscovery
from .material_reader import _validate_url
from .supply_evidence import group_independence
from .supply_layered_plan import LayeredPlanModel
from .supply_layered_sources import PlacesEvidence, rendered_source
from .supply_read_order import schedule_reads
from .deepseek_discovery import DeepSeekSearchResponse, DiscoveryHint
from .supply_research import SupplyResearch
from .supply_render_queue import RenderQueueClient
from .supply_jina_reader import JinaReader


def https_lead(url):
    """Only change scheme on the identical public host; still a discovery lead."""
    parsed = urlsplit(url)
    candidate = urlunsplit(parsed._replace(scheme='https')) if parsed.scheme == 'http' else url
    return _validate_url(candidate).geturl()


class UpgradingDiscovery:
    def __init__(self, discovery, directory):
        self.discovery, self.directory = discovery, Path(directory)

    def __call__(self, *args, **kwargs):
        response = self.discovery(*args, **kwargs)
        changes = []

        def upgrade(url, occurrence):
            try:
                result = https_lead(url)
            except (ValueError, TypeError, UnicodeError):
                result = url  # Existing queue records the rejection.
            if result != url:
                changes.append({'occurrence': occurrence, 'original_url': url,
                                'candidate_url': result, 'reason': 'authorized_same_host_https_upgrade'})
            return result

        urls = tuple(upgrade(url, f'result-{i}') for i, url in enumerate(response.urls, 1))
        leads = tuple(replace(lead, candidate_url=upgrade(lead.candidate_url, f'summary-{i}'),
                              normalization=lead.normalization + ('layered_upgrade_checked',))
                      for i, lead in enumerate(response.summary_leads, 1))
        private_json(self.directory / 'url-upgrades.json', changes)
        return replace(response, urls=urls, summary_leads=leads)


class BrowserEvidenceBridge:
    """A bounded file handoff to the operator's rendered-browser tool.

    This does not pretend a backend browser exists. The request and actual
    captured DOM provenance are durable; no capture means no original source.
    """
    def __init__(self, directory, *, timeout_seconds=600):
        self.directory, self.timeout_seconds = Path(directory), timeout_seconds

    def __call__(self, *, seed, plan, urls, request_id):
        directory = self.directory / 'browser-jobs' / hashlib.sha256(request_id.encode()).hexdigest()
        safe, excluded = [], []
        for url in dict.fromkeys(urls):
            try:
                candidate = https_lead(url)
            except (ValueError, TypeError, UnicodeError):
                excluded.append({'url': url, 'reason': 'public_url_validation_rejected'})
                continue
            if candidate not in safe:
                safe.append(candidate)
        # Official hints affect ordering only, not evidence qualification.
        response = DeepSeekSearchResponse(tuple(safe), 'response_received',
                    tuple(DiscoveryHint('Official website lead', '') for _ in safe))
        schedule = schedule_reads(response, safe)
        requested_urls = [r['requested_url'] for r in schedule[:6]]
        private_json(directory / 'request.json', {
            'request_id': request_id, 'case_id': seed['id'], 'created_at': now(),
            'decision_need': seed['decision_need'], 'questions': plan['official_questions'],
            'urls': requested_urls, 'schedule': schedule,
            'not_attempted': [{'url': r['requested_url'], 'reason': 'browser_page_limit'}
                              for r in schedule[6:]],
            'excluded': excluded, 'page_limit': 6, 'tool': 'rendered_browser',
            'identity': 'must be supported by captured visible text, never domain alone'})
        target = directory / 'captures.json'
        expires = time.monotonic() + self.timeout_seconds
        while not target.exists() and time.monotonic() < expires:
            time.sleep(0.25)
        if not target.exists():
            private_json(directory / 'failure.json', {'reason': 'browser_tool_not_completed', 'at': now()})
            return {'sources': [], 'ledger': [{'source_layer': 'official_rendered',
                    'read_status': 'unavailable', 'reason': 'browser_tool_not_completed'}]}
        captured = json.loads(target.read_text())
        if captured.get('request_id') != request_id or not isinstance(captured.get('captures'), list):
            raise ValueError('Browser response does not match request')
        if len(captured['captures']) > 6:
            raise ValueError('Browser page allowance exceeded')
        sources, ledger, read_parents = [], [], {}
        for capture in captured['captures']:
            requested = capture.get('requested_url')
            parent = read_parents.get(capture.get('discovered_from'), {})
            links = parent.get('visible_links', [])
            visible_child = (isinstance(requested, str) and bool(requested)
                             and isinstance(links, list)
                             and all(isinstance(link, str) for link in links)
                             and requested in links)
            # Children require an earlier captured page, not a model-supplied parent URL.
            if requested not in requested_urls and not visible_child:
                raise ValueError('Browser capture has no queued parent')
            result = rendered_source(capture)
            ledger.append(result['ledger'])
            if result['source'] is not None:
                sources.append(result['source'])
                read_parents[capture.get('url')] = capture
        private_json(directory / 'source-ledger.json', ledger)
        return {'sources': sources, 'ledger': ledger}


class OfficialReader:
    """Read official leads via Jina, retaining the existing renderer as fallback."""
    def __init__(self, directory, *, jina=None, renderer=None, budget=None):
        self.directory = Path(directory)
        assessor = None
        if budget is not None:
            from .supply_source_identity import SourceIdentityModel
            assessor = SourceIdentityModel(budget)
        self.jina = jina if jina is not None else JinaReader(directory)
        self.identity_reader = JinaReader(directory, identity_assessor=assessor) if assessor else None
        self.renderer = renderer if renderer is not None else RenderQueueClient(directory)

    def __call__(self, *, seed, plan, urls, request_id):
        result = self.jina(seed=seed, plan=plan, urls=urls, request_id=request_id)
        fallback = result.get('fallback_urls', [])
        receipt = {'request_id': request_id, 'started_at': now(), 'tool_cost': 'unknown',
                   'policy': 'official leads first; Jina then eligible renderer fallback',
                   'fallback_urls': fallback, 'terminal_blocked_urls': result.get('terminal_blocked_urls', []),
                   'render_status': 'pending' if fallback else 'not_needed',
                   'jina_receipt': result.get('tool_receipt')}
        path = self.directory / 'reader-routing' / hashlib.sha256(request_id.encode()).hexdigest()[:20] / 'receipt.json'
        private_json(path, receipt)
        rendered = {'sources': [], 'ledger': []}
        if fallback:
            try:
                rendered = self.renderer(seed=seed, plan=plan, urls=fallback,
                                         request_id=request_id + '-render-fallback')
                receipt.update(render_status='received', render_receipt=rendered.get('tool_receipt'))
            except Exception as error:
                receipt.update(render_status='failed', raw_error={
                    'type': type(error).__name__, 'message': str(error)})
                rendered['ledger'].append({'source_layer': 'official_rendered',
                    'read_status': 'unavailable', 'reason': 'render_fallback_failed',
                    'routing_receipt_path': str(path)})
        receipt['finished_at'] = now()
        private_json(path, receipt)
        collected = {'sources': result['sources'] + rendered['sources'],
                     'ledger': result['ledger'] + rendered['ledger'], 'reader_routing': receipt}
        if self.identity_reader is not None:
            from .supply_jina_identity import assess_original_sources
            identity_dir = self.directory / 'publisher-assessment' / hashlib.sha256(request_id.encode()).hexdigest()[:20]
            collected = assess_original_sources(self.identity_reader, collected, identity_dir, request_id)
        return collected


class LayeredResearch:
    def __init__(self, budget, *, planner=None, places=None, browser=None, search_factory=None):
        self.budget = budget
        self.planner = planner or LayeredPlanModel(budget)
        self.places = places or PlacesEvidence(budget.directory / 'structured-tools')
        self.browser = browser if browser is not None else OfficialReader(budget.directory, budget=budget)
        self.search_factory = search_factory or self._search_factory

    def _search_factory(self, query, directory):
        search = DeepSeekDiscovery(query=query['query'], context=query['context'])
        return SupplyResearch(self.budget, searcher=UpgradingDiscovery(search, directory))

    def __call__(self, seed, *, request_id):
        directory = self.budget.directory / 'layered' / seed['id']
        plan = self.planner(seed, request_id=request_id + '-plan')
        private_json(directory / 'plan.json', plan)
        collected = []
        for i, candidate in enumerate(plan['candidates']):
            result = self.places(candidate['name'], candidate['destination'],
                                 request_id=request_id + f'-place-{i}')
            identity = {'canonical_name': candidate['name'],
                        'display_name': candidate.get('display_name'),
                        'destination': candidate['destination'],
                        'basis': 'explicit_planner_lookup_not_verified_identity'}
            result['planner_identity'] = identity
            for row in result.get('ledger', []):
                row['planner_identity'] = copy.deepcopy(identity)
            collected.append(result)
        private_json(directory / 'structured-results.json', collected)
        urls = [url for row in collected for url in row.get('official_urls', [])]
        urls += plan.get('official_url_leads', [])
        browser = self.browser(seed=seed, plan=plan, urls=urls, request_id=request_id)
        if not any(source.get('original_read_status') == 'read'
                   and str(source.get('content') or '').strip()
                   for source in browser.get('sources', [])):
            private_json(directory / 'failure.json', {
                'status': 'failed', 'reason': 'official_rendered_body_missing',
                'request_id': request_id, 'finished_at': now(),
                'search_attempted': False, 'candidate_generation_attempted': False,
                'source_ledger': browser.get('ledger', [])})
            raise RuntimeError('official_rendered_body_missing: stopped before Search and candidates')
        query = plan['search_queries'][plan['selected_query_index']]
        enriched = self.search_factory(query, directory)(seed, request_id=request_id + '-experience')
        for source in enriched['sources']:
            source['source_layer'] = 'experience_original'
        for row in enriched['source_ledger']:
            row['source_layer'] = 'experience_original'
        additions = [*collected, browser]
        enriched['sources'] = [source for result in additions for source in result.get('sources', [])] + enriched['sources']
        enriched['source_ledger'] = [row for result in additions for row in result.get('ledger', [])] + enriched['source_ledger']
        # Repeated official pages or upstream references cannot become independent.
        enriched['sources'] = list({source['id']: source for source in enriched['sources']}.values())
        assessable = [row for row in enriched['source_ledger'] if 'source_id' in row and 'url' in row]
        group_independence(enriched['sources'], assessable)
        enriched['research_context']['layered'] = {
            'plan': plan, 'selected_query': query,
            'browser_execution': 'Jina original text with eligible ops render queue fallback',
            'structured_policy': 'Provider-scoped fields, not official inventory or entry guarantees.',
            'official_policy': 'Visible original text; precise entity/date/claim agreement still required.',
            'experience_policy': 'Search is discovery only; original, freshness and upstream checks retained.',
            'tool_calls_not_product_calls': True}
        private_json(directory / 'enriched.json', enriched)
        return enriched
