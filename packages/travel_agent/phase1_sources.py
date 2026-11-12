"""Bind only cited source dependencies to review, then recheck in commit's transaction."""
import hashlib
import json
from copy import deepcopy
from . import detail_public_cache
from .detail_reviews import source_withdrawn
from .multisource_organize import _reference_key
from .store import StateError


def _aliases(row):
    return {_reference_key(value) for value in [row.get(key) for key in
        ('url', 'source_url', 'requested_url', 'product_url')] + list(row.get('redirect_chain') or [])} - {None}


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def source_bindings(trip, refs):
    rows = [row for row in trip.get('source_evidence', []) if isinstance(row, dict)]
    receipts = [dict(row, status='blocked') if source_withdrawn(row) or row.get('status') == 'withdrawn'
                else row for row in rows]
    blocked = detail_public_cache.blocked_urls(detail_public_cache.remember_failures(
        trip.get('detail_public_cache'), receipts))
    supplies = [row for row in (trip.get('plan') or {}).get('supplies', []) if isinstance(row, dict)]
    result = []
    for ref in sorted(set(refs)):
        key = _reference_key(ref)
        if key is None:
            continue  # Non-URL offline receipt ids have no mutable public-source dependency.
        matching = [row for row in rows if key in _aliases(row)]
        metadata = [{k: row.get(k) for k in ('id', 'source_url', 'product_url', 'source_provenance')}
                    for row in supplies if key in _aliases(row)]
        result.append({'source_ref': ref, 'blocked': key in blocked,
                       'state_sha256': _fingerprint([matching, metadata])})
    return result


def review_refs(review):
    refs = []
    for row in (review.get('blind_spots') or {}).get('candidates', []):
        refs.extend(row.get('source_refs', []))
        for evidence in row.get('evidence', []):
            receipt = evidence.get('tool_receipt') or {}
            refs.extend([evidence.get('source_ref'), receipt.get('requested_url'), receipt.get('url')])
            refs.extend(receipt.get('redirect_chain') or [])
    return [ref for ref in refs if isinstance(ref, str)]


def preserve_source_failures(current, incoming):
    """Current receipts must survive a stale whole-list publication.

    Keep the current negative receipt (including its body for audit); never
    promote it to read because another cited source passed review.
    """
    result = deepcopy(incoming)
    current_ids = {row['id']: row for row in current if row.get('id')}
    for index, row in enumerate(result):
        prior = current_ids.get(row.get('id'))
        code = row.get('http_status')
        incoming_negative = (source_withdrawn(row) or row.get('status') in ('withdrawn', 'blocked', 'unavailable', 'failed')
                             or (type(code) is int and code != 200))
        if prior is not None and not incoming_negative:
            result[index] = deepcopy(prior)
    for row in current:
        if not any((row.get('id') and value.get('id') == row['id']) or value == row for value in result):
            result.append(deepcopy(row))
        code = row.get('http_status')
        if (source_withdrawn(row) or row.get('status') in ('withdrawn', 'blocked', 'unavailable', 'failed')
                or (type(code) is int and code != 200)):
            match = next((index for index, value in enumerate(result) if row.get('id')
                          and value.get('id') == row['id']), None)
            if match is not None:
                result[match] = deepcopy(row)
            elif row not in result:
                result.append(deepcopy(row))
    return result


def assert_review_sources(trip, review):
    if not isinstance(review, dict):
        return
    current = source_bindings(trip, review_refs(review))
    if any(row['blocked'] for row in current):
        raise StateError('STALE_EVIDENCE', '交付前引用来源已失效或撤回，已有方案保留', 409)
    expected = review.get('source_bindings')
    if expected is not None and current != expected:
        raise StateError('STALE_EVIDENCE', '核验后的来源版本已变化，已有方案保留', 409)
