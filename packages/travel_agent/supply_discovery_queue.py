"""Per-occurrence discovery accounting; summary links remain untrusted leads."""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlsplit

from . import public_search


URL_TOKEN = re.compile(
    r'[a-z][a-z0-9+.-]*://[^\s<>"\x27`，。；：！？（）【】]*'
    r'|(?<![a-z0-9_@./:-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+'
    r'[a-z]{2,63}(?::[0-9]+)?(?:[/?#][^\s<>"\x27`，。；：！？（）【】]*)?', re.I)
OFFICIAL_CONTEXT = r'(?:official\s+(?:web\s?site|site|operator)|官方网站|官方網站|官网|官網)'
AUTHORITY_CONTEXT = r'(?:government|authority|ministry|政府|管理局|旅游局|旅遊局)'
CONTEXT_SEPARATORS = r'[\s:：()\[\]（）【】|—–-]*'


@dataclass(frozen=True)
class SummaryLead:
    raw_url: str
    candidate_url: str
    parent_search_rank: int
    parent_url: str
    token_start: int
    token_end: int
    excerpt: str
    excerpt_start: int
    excerpt_end: int
    normalization: tuple[str, ...] = ()
    context_label: str = ''


def _trim_punctuation(raw):
    value = raw.rstrip('.,;!?…')
    for opening, closing in (('(', ')'), ('[', ']'), ('{', '}')):
        while value.endswith(closing) and value.count(closing) > value.count(opening):
            value = value[:-1]
    return value


def _context_label(prefix, suffix):
    for pattern, label in ((AUTHORITY_CONTEXT, 'Authority'), (OFFICIAL_CONTEXT, 'Official website')):
        if (re.search(pattern + CONTEXT_SEPARATORS + '$', prefix, re.I)
                or re.match(CONTEXT_SEPARATORS + r'(?:is\s+(?:the\s+)?)?' + pattern, suffix, re.I)):
            return label
    return ''


def summary_leads(results):
    """Extract explicit URL/domain tokens with bounded context, never body facts."""
    leads = []
    for rank, item in enumerate(results, 1):
        summary = item.get('summary')
        if not isinstance(summary, str):
            continue
        for match in URL_TOKEN.finditer(summary):
            raw, start, end = match.group(), match.start(), match.end()
            candidate, normalization = _trim_punctuation(raw), []
            if candidate != raw:
                normalization.append('display_punctuation_trimmed')
            if '://' not in candidate:
                candidate = 'https://' + candidate
                normalization.append('schemeless_https_candidate')
            left, right = max(0, start - 60), min(len(summary), end + 60, start + 300)
            leads.append(SummaryLead(raw, candidate, rank, item['url'], start, end,
                summary[left:right], left, right, tuple(normalization),
                _context_label(summary[max(0, start - 100):start], summary[end:end + 100])))
    return tuple(leads)


def _admit(candidate):
    try:
        scheme = urlsplit(candidate).scheme.lower()
        if scheme == 'http':
            return None, 'http_not_allowed'
        if scheme != 'https':
            return None, 'unsupported_or_missing_scheme'
        parsed = public_search._validate_url(candidate)
    except (ValueError, TypeError, UnicodeError):
        return None, 'public_url_validation_rejected'
    if (parsed.hostname or '').lower() in public_search.BLOCKED_HOSTS:
        return None, 'blocked_search_host'
    if parsed.hostname == 'vertexaisearch.cloud.google.com':
        return None, 'unresolved_attribution_url'
    return parsed.geturl(), 'validated_public_https'


def discovery_queue(response):
    """Account every top-level/summary occurrence before deduplicating the queue."""
    rows = [{'discovery_id': 'result-' + str(rank), 'origin': 'search_result_url',
             'parent_search_rank': rank, 'parent_url': raw, 'raw_url': raw,
             'candidate_url': raw, 'normalization': []}
            for rank, raw in enumerate(response.urls, 1)]
    for index, lead in enumerate(response.summary_leads, 1):
        rows.append({'discovery_id': 'summary-' + str(index), 'origin': 'summary_extraction',
                     'parent_search_rank': lead.parent_search_rank, 'parent_url': lead.parent_url,
                     'raw_url': lead.raw_url, 'candidate_url': lead.candidate_url,
                     'token_start': lead.token_start, 'token_end': lead.token_end,
                     'excerpt': lead.excerpt, 'excerpt_start': lead.excerpt_start,
                     'excerpt_end': lead.excerpt_end, 'excerpt_status': 'discovery_only_not_evidence',
                     'normalization': list(lead.normalization)})
    queued = {}
    for row in rows:
        url, reason = _admit(row['candidate_url'])
        row.update(requested_url=url, admission_reason=reason, duplicate_of=None)
        if url is None:
            row['admission_status'] = 'rejected'
        elif url in queued:
            row.update(admission_status='duplicate', admission_reason='duplicate_url',
                       duplicate_of=queued[url])
        else:
            row['admission_status'] = 'queued'
            queued[url] = row['discovery_id']
        if url and url != row['candidate_url']:
            row['normalization'].append('reader_url_canonicalization')
    return list(queued), rows


def queue_read_receipts(admissions, ledger):
    """Join the queue audit to existing source receipts without adding sources."""
    reads = {row['requested_url']: row for row in ledger}
    for row in admissions:
        read = reads.get(row['requested_url'], {})
        duplicate = row['admission_status'] == 'duplicate'
        row.update(scheduled_read_order=read.get('scheduled_read_order'),
                   actual_read_order=None if duplicate else read.get('actual_read_order'),
                   shared_read_order=read.get('actual_read_order') if duplicate else None,
                   reader_invoked=bool(not duplicate and read.get('reader_invoked')),
                   read_status=read.get('read_status', 'not_attempted'),
                   read_reason=read.get('reason') or row['admission_reason'])
    return admissions
