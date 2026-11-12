"""Read an already admitted public source for a blind spot, within its fact budget.

Lookup completion is not semantic confirmation. The sixth critic receives the
actual source excerpt; unresolved hypotheses stay visibly conditional.
"""
from __future__ import annotations

from urllib.parse import urlsplit
from copy import deepcopy

from .material_reader import read_public_link
from . import detail_public_cache
from .detail_reviews import source_withdrawn
from .multisource_organize import _reference_key, _protected_references
from .phase1_budget_context import use_budget
from .source_policy_view import source_policy_view


class Phase1SourceResolver:
    manages_budget = True

    def __init__(self, trip, *, reader=None, trip_loader=None):
        self.reader = reader or read_public_link
        self.trip, self.trip_loader = trip, trip_loader
        self.refresh()

    def refresh(self):
        trip = self.trip_loader() if self.trip_loader else self.trip
        self.source_snapshot = deepcopy({'source_evidence': trip.get('source_evidence', []),
            'detail_public_cache': trip.get('detail_public_cache'),
            'plan': {'supplies': (trip.get('plan') or {}).get('supplies', [])}})
        plan = trip.get('plan') or {}
        raw_sources = [row for row in trip.get('source_evidence', []) if isinstance(row, dict)]
        receipts = [dict(row, status='blocked') if source_withdrawn(row) or row.get('status') == 'withdrawn'
                    else row for row in raw_sources]
        cache = detail_public_cache.remember_failures(trip.get('detail_public_cache'), receipts)
        blocked = detail_public_cache.blocked_urls(cache)
        # These reviewed, user-shared adapters have no new acquisition authority.
        blocked |= _protected_references(plan.get('supplies') or [], raw_sources)
        context = source_policy_view({'supplies': plan.get('supplies') or [],
            'source_evidence': trip.get('source_evidence') or []})
        known = {}
        for row in context.get('source_evidence', []):
            url = row.get('url') or row.get('source_url')
            if not url:
                continue
            if row.get('status') == 'read':
                known[url] = row.get('title') or '已读取公开来源'
        for supply in context.get('supplies', []):
            for key in ('source_url', 'product_url'):
                url = supply.get(key)
                if url:
                    known.setdefault(url, supply.get('name') or supply.get('title') or '方案公开来源')
        self.sources = [{'source_ref': url, 'title': title} for url, title in known.items()
                        if _reference_key(url) is not None and _reference_key(url) not in blocked
                        and urlsplit(url).scheme == 'https'
                        and not urlsplit(url).username]
        self.allowed = {row['source_ref'] for row in self.sources}
        self.lookup_capabilities = {'mode': 'admitted_public_sources_only',
            'allowed_source_refs': sorted(self.allowed), 'can_search': False,
            'can_contact': False, 'can_book': False, 'can_pay': False}

    def __call__(self, candidate, lease):
        self.refresh()
        url = str(candidate.get('query') or '').strip()
        pending = {'disposition': 'needs_attention', 'owner': candidate['owner'],
            'next_action': candidate['next_action'], 'source_refs': [], 'evidence': []}
        if url not in self.allowed:
            return {**pending, 'reason': '当前没有该盲区可直接读取的已知来源入口；未发送个人资料或猜测网址',
                'lookup_receipt': {'status': 'not_attempted', 'reason_code': 'source_not_admitted',
                                   'attempted': False, 'source_ref': ''}}
        with use_budget(lease.budget):
            receipt = self.reader(url, deadline=lease.deadline)
        if receipt.get('status') != 'read' or not receipt.get('text'):
            return {**pending, 'reason': receipt.get('message') or '公开来源未返回可核验正文',
                'lookup_receipt': {'status': 'failed', 'reason_code': 'source_read_failed',
                                   'attempted': True, 'source_ref': url}}
        source = receipt.get('url') or url
        return {'disposition': 'checked', 'owner': candidate['owner'], 'next_action': candidate['next_action'],
            'reason': '已读取公开原文，具体适用性由第六项语义核对判断；尚不构成事实确认',
            'lookup_receipt': {'status': 'read', 'reason_code': 'source_body_read',
                               'attempted': True, 'source_ref': url},
            'source_refs': [source], 'evidence': [{'source_ref': source,
                'excerpt': receipt['text'][:1500], 'result_status': 'success',
                'body_sha256': receipt.get('body_sha256'), 'observed_at': receipt.get('fetched_at'),
                'truncated': bool(receipt.get('truncated') or len(receipt['text']) > 1500),
                'tool_receipt': {key: receipt.get(key) for key in ('requested_url', 'redirect_chain', 'http_status')}}]}
