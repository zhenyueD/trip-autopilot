"""Untrusted discovery likelihood only schedules reads; it cannot certify sources."""
from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from . import public_search
from .deepseek_discovery import DeepSeekSearchResponse
from .supply_evidence import is_reddit_post


OFFICIAL_LABEL = re.compile(
    r'(?:\bofficial\s+(?:web\s?site|site|operator)|官方网站|官方網站|官网|官網)'
    r'(?:\s*[-|:：—–].*)?$', re.I)
AUTHORITY_LABEL = re.compile(
    r'\b(?:authority|government|ministry|municipality)\b|旅游局|旅遊局|管理局|政府', re.I)
VISITOR_PATHS = {'plan-your-visit', 'visitor-information', 'opening-hours', 'visit-us'}
PRIORITIES = {'likely_official_or_first_party': 0, 'likely_authority': 1, 'other': 2}


def _canonical_url(raw):
    try:
        return public_search._validate_url(raw).geturl()
    except (ValueError, UnicodeError, TypeError):
        return None


def _discovery_labels(response, urls):
    """Associate labels with the same validated URL; never parse search prose."""
    ranks = {url: index for index, url in enumerate(urls, 1)}
    labels = {}
    if isinstance(response, DeepSeekSearchResponse):
        ranks = {}
        for index, raw in enumerate(response.urls, 1):
            url = _canonical_url(raw)
            if url not in urls or url in ranks:
                continue
            ranks[url] = index
            if index <= len(response.discovery_hints):
                hint = response.discovery_hints[index - 1]
                labels[url] = {'search_title': hint.title, 'search_site_name': hint.site_name}
        for lead in response.summary_leads:
            url = _canonical_url(lead.candidate_url)
            if url not in urls:
                continue
            ranks.setdefault(url, lead.parent_search_rank)
            if lead.context_label:
                labels.setdefault(url, {})['summary_context'] = lead.context_label
    elif isinstance(response, dict):
        for candidate in response.get('candidates', []) or []:
            if not isinstance(candidate, dict):
                continue
            metadata = candidate.get('groundingMetadata') or {}
            if not isinstance(metadata, dict):
                continue
            chunks = metadata.get('groundingChunks')
            for chunk in chunks if isinstance(chunks, list) else []:
                web = chunk.get('web') if isinstance(chunk, dict) else None
                if not isinstance(web, dict):
                    continue
                url = _canonical_url(web.get('uri'))
                title = web.get('title')
                if url in urls and isinstance(title, str):
                    labels.setdefault(url, {'search_title': title[:300]})
    return ranks, labels


def _priority(url, labels):
    parsed = urlsplit(url)
    host_parts = set(re.split(r'[.\-]', (parsed.hostname or '').lower()))
    path_parts = set(unquote(parsed.path).lower().split('/'))
    official, authority = [], []
    if host_parts.intersection({'official', 'operator'}):
        official.append('url_host:official_or_operator_marker')
    if path_parts.intersection(VISITOR_PATHS):
        official.append('url_path:visitor_information_marker')
    if host_parts.intersection({'gov', 'government', 'authority'}):
        authority.append('url_host:authority_marker')
    for key, value in labels.items():
        if OFFICIAL_LABEL.search(value):
            official.append(key + ':official_site_marker')
        if AUTHORITY_LABEL.search(value):
            authority.append(key + ':authority_marker')
    # Government identity hints take the authority bucket even if its title says
    # "official website". Both buckets still require body-based authentication.
    if authority:
        return 'likely_authority', authority + official
    if official:
        return 'likely_official_or_first_party', official
    return 'other', ['no_first_party_or_authority_hint']


def schedule_reads(response, urls):
    """Keep validated/deduplicated URLs and return a fully traceable read schedule."""
    ranks, labels = _discovery_labels(response, urls)
    first_reddit = next((url for url in urls if is_reddit_post(url)), None)
    rows = []
    for index, url in enumerate(urls, 1):
        likelihood, reasons = _priority(url, labels.get(url, {}))
        reddit_boost = (url == first_reddit if isinstance(response, DeepSeekSearchResponse)
                        else is_reddit_post(url))
        rows.append({'requested_url': url, 'discovery_rank': ranks.get(url, index),
                     'priority_likelihood': likelihood, 'priority_reasons': reasons,
                     'actual_read_order': None, '_reddit_boost': reddit_boost})
    rows.sort(key=lambda row: (PRIORITIES[row['priority_likelihood']],
                              not row['_reddit_boost'], row['discovery_rank']))
    for index, row in enumerate(rows, 1):
        row.pop('_reddit_boost')
        row['scheduled_read_order'] = index
    return rows
