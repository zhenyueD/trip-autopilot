"""Mechanical read provenance, recency and known shared-origin grouping.

Eligibility is not verification. Reader metadata may establish source kind or
dates; discovery prose, destination labels and publisher counts cannot.
"""
from __future__ import annotations

import copy
import hashlib
import re
from datetime import datetime

from .material_reader import _validate_url

SOURCE_TYPES = frozenset({'official', 'operator', 'authority', 'maps_routes',
                          'reddit_post', 'social_post', 'review', 'unknown'})
SOCIAL_TYPES = frozenset({'reddit_post', 'social_post', 'review'})
PRIMARY_TYPES = {'official': ['hard_fact'], 'operator': ['hard_fact'],
                 'authority': ['permission'], 'maps_routes': ['transport']}


def content_backed_type(document, content):
    """Trace a reader's type evidence to this body, without inferring ownership.

    Existing string quotes and structured quote/excerpt evidence are accepted.
    A domain, type label, or search assessment is not publisher provenance.
    Optional fingerprints constrain the quote to the captured document.
    """
    provenance = document.get('source_provenance')
    provenance = provenance if isinstance(provenance, dict) else {}
    proof = provenance.get('source_type_evidence', document.get('source_type_evidence'))
    quote = proof if isinstance(proof, str) else None
    if isinstance(proof, dict):
        if any(word in str(proof.get('basis', '')).lower() for word in ('domain', 'search', 'url')):
            return False
        quote = next((proof[key] for key in ('quote', 'direct_quote', 'excerpt')
                      if isinstance(proof.get(key), str)), None)
    if not isinstance(content, str) or not isinstance(quote, str):
        return False
    body, quote = ' '.join(content.split()), ' '.join(quote.split())
    labels = {'official', 'operator', 'authority', 'official website', 'official page',
              'official site', '官网', '官方', '官方网站', '官方页面', '经营者', '主管机关'}
    if not quote or quote.casefold() in labels or quote not in body:
        return False
    if isinstance(proof, dict):
        for key, actual in (('content_sha256', hashlib.sha256(body.encode()).hexdigest()),
                            ('body_sha256', document.get('body_sha256'))):
            if key in proof and (not isinstance(proof[key], str) or not actual
                                 or proof[key].lower() != actual.lower()):
                return False
    return True


def lead_only_reason(document, content):
    """Recognize explicit page labels/reader provenance, not source reputation."""
    if document.get('lead_only') is True:
        return document.get('lead_only_reason') or 'reader_declared_lead_only'
    provenance = document.get('source_provenance')
    provenance = provenance if isinstance(provenance, dict) else {}
    if any(provenance.get(key, document.get(key)) for key in
           ('ai_generated', 'is_aggregated', 'repost_of', 'translated_from', 'upstream_url')):
        return 'reader_declared_generated_or_derivative'
    if provenance.get('source_type', document.get('source_type')) in {
            'aggregator', 'aggregation', 'aigc', 'ai_generated'}:
        return 'reader_declared_generated_or_derivative'
    body = re.sub(r'\s+', '', content).casefold() if isinstance(content, str) else ''
    labels = ('aigc文章', 'aigcarticle', 'aigccontent', 'ai生成', 'ai-generated', 'aigenerated',
              'generatedbyai', '人工智能生成', '聚合文章', '聚合内容', '聚合帖',
              'aggregatedcontent', 'aggregatedfrom')
    if any(label in body for label in labels) or re.search(r'源自\d+位全网作者', body):
        return 'body_self_labelled_generated_or_aggregated'
    return None


def public_url(value):
    try:
        return _validate_url(value).geturl()
    except (ValueError, TypeError, UnicodeError):
        return None


def is_reddit_post(url):
    canonical = public_url(url)
    return bool(canonical and _validate_url(canonical).hostname in {'reddit.com', 'www.reddit.com'})


def source_kind(document, url):
    if is_reddit_post(url):
        return 'reddit_post', {'basis': 'validated_public_post_url', 'url': url}
    provenance = document.get('source_provenance')
    provenance = provenance if isinstance(provenance, dict) else {}
    kind = provenance.get('source_type', document.get('source_type'))
    evidence = provenance.get('source_type_evidence', document.get('source_type_evidence'))
    if isinstance(kind, str) and kind in SOURCE_TYPES and isinstance(evidence, (dict, str)) and evidence:
        return kind, copy.deepcopy(evidence)
    return 'unknown', None


def _stamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _age_status(value, captured, days):
    published, retrieved = _stamp(value), _stamp(captured)
    if published is None or retrieved is None:
        return 'unknown'
    age = (retrieved - published).total_seconds()
    if age < 0:
        return 'future'
    return 'recent' if age <= days * 86400 else 'outdated'


def _freshness(document, provenance, kind):
    published = provenance.get('published_at', document.get('published_at'))
    experienced = provenance.get('experienced_at', document.get('experienced_at'))
    captured = document.get('fetched_at')
    days = 30 if kind == 'review' else 7 if kind in SOCIAL_TYPES else None
    status = _age_status(published, captured, days) if days else 'not_applicable'
    experience_status = _age_status(experienced, captured, days) if days else 'not_applicable'
    eligible = (document.get('status') == 'read' and status == 'recent'
                and (experienced is None or experience_status == 'recent'))
    return {'retrieved_at': captured, 'published_at': published, 'experienced_at': experienced,
            'freshness_status': status, 'experience_freshness_status': experience_status,
            'freshness_basis': 'reader_published_at' if published is not None and days else None,
            'freshness_window_days': days, 'freshness_eligible': bool(eligible)}


def _origin_refs(document, provenance):
    values = [provenance.get(key, document.get(key)) for key in
              ('canonical_url', 'upstream_url', 'translated_from', 'repost_of')]
    return list(dict.fromkeys(url for value in values if (url := public_url(value))))


def _policy(kind, read, freshness, provenance):
    # This is temporal eligibility, not a semantic first-hand/truth gate.
    current = bool(read and freshness['freshness_eligible'] and kind in SOCIAL_TYPES)
    primary = copy.deepcopy(PRIMARY_TYPES.get(kind, [])) if read else []
    if current:
        primary.append('experience')
    reason = ('original_not_read' if not read else 'source_type_needs_content_assessment' if kind == 'unknown'
              else 'recent_experience_candidate_firsthand_unassessed' if current
              else 'freshness_not_established' if kind in SOCIAL_TYPES and not freshness['freshness_eligible']
              else 'typed_primary_candidate_not_verified')
    return {'primary_for': primary, 'current_experience_eligible': current,
            'freshness_eligible': freshness['freshness_eligible'],
            'background_only': not read or (kind in SOCIAL_TYPES and not current),
            'social_unverified': kind in SOCIAL_TYPES,
            'firsthand_status': 'unassessed', 'applicability_status': 'unassessed',
            'reason': reason}


def evidence_fields(document, *, source_id, url):
    """Use original-reader metadata only, including on unsuccessful reads."""
    provenance = document.get('source_provenance')
    provenance = provenance if isinstance(provenance, dict) else {}
    kind, type_evidence = source_kind(document, url)
    freshness = _freshness(document, provenance, kind)
    content = document.get('text')
    read = document.get('status') == 'read' and isinstance(content, str) and bool(content.strip())
    if not read:
        freshness['freshness_eligible'] = False
    policy = _policy(kind, read, freshness, provenance)
    body_hash = document.get('body_sha256')
    if not isinstance(body_hash, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', body_hash):
        body_hash = None
    content_hash = hashlib.sha256(' '.join(content.split()).encode()).hexdigest() if read else None
    content_complete = bool(read and not document.get('truncated') and document.get('content_complete') is not False)
    lead_reason = lead_only_reason(document, content)
    return {'source_id': source_id, 'url': url, 'source_type': kind,
            'source_type_evidence': type_evidence, 'read_status': document.get('status', 'unknown'),
            'source_type_content_bound': bool(read and content_backed_type(document, content)),
            'lead_only': bool(lead_reason), 'lead_only_reason': lead_reason,
            **freshness, 'current_experience_eligible': policy['current_experience_eligible'],
            'evidence_policy': policy, 'body_sha256': body_hash.lower() if body_hash else None,
            'content_sha256': content_hash, 'content_complete': content_complete,
            'content_fingerprint_scope': ('complete_returned_visible_text' if content_complete else 'excerpt') if read else None,
            'origin_refs': _origin_refs(document, provenance),
            'independence_group': 'unassessed-' + source_id, 'independence_status': 'unassessed',
            'independence_reason': 'Different publishers or domains do not establish independence.',
            'reason': document.get('reason') or policy['reason']}


def group_independence(sources, ledger):
    """Collapse known duplicate fingerprints and declared original-source links.

    Unmatched groups stay unassessed. This never establishes independent
    corroboration, semantic agreement, or authenticity of external assertions.
    """
    parents = list(range(len(ledger)))

    def root(index):
        while parents[index] != index:
            index = parents[index]
        return index

    seen = {}
    for index, row in enumerate(ledger):
        identities = [('url', url) for url in [row['url'], *row.get('origin_refs', [])]]
        if row.get('body_sha256'):
            identities.append(('body', row['body_sha256']))
        if row.get('content_complete') and row.get('content_sha256'):
            identities.append(('content', row['content_sha256']))
        for identity in identities:
            if identity in seen:
                parents[root(index)] = root(seen[identity])
            else:
                seen[identity] = index
    members = {}
    for index in range(len(ledger)):
        members.setdefault(root(index), []).append(index)
    by_id = {source['id']: source for source in sources}
    for indices in members.values():
        grouped = len(indices) > 1 or any(ledger[index].get('origin_refs') for index in indices)
        group_id = ('same-upstream-' if grouped else 'unassessed-') + ledger[indices[0]]['source_id']
        for index in indices:
            row = ledger[index]
            row.update(independence_group=group_id,
                       independence_status='same_upstream' if grouped else 'unassessed')
            if grouped:
                row['independence_reason'] = 'Matching captured body fingerprint, URL, or declared upstream; not independent.'
            if row['source_id'] in by_id:
                by_id[row['source_id']].update({key: row[key] for key in
                                              ('independence_group', 'independence_status', 'independence_reason')})
