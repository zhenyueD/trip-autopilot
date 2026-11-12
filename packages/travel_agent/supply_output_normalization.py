"""Lossless model evidence containers and source-bound canonical temporal values.

The advertised strict schema remains the boundary. Only unambiguous evidence
containers are adapted; unknown fields and missing witnesses are never repaired.
Whitespace-equivalent quotes may bind only to unique exact source spans.
Sidecar provenance records representations, not semantic truth.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from .supply_normalization import normalize_temporal, temporal_assertion_text, covers_date


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def normalize_output_envelope(raw, *, expected_fields):
    """Unwrap one unambiguous object envelope; leave strict validation unchanged."""
    value, changes = copy.deepcopy(raw), []
    fields = set(expected_fields)
    if isinstance(value, dict) and len(value) == 1:
        wrapper, payload = next(iter(value.items()))
        if (wrapper not in fields and isinstance(payload, dict)
                and payload and set(payload) <= fields):
            value = payload
            changes.append({'path': '$', 'source_path': f'$[{json.dumps(wrapper, ensure_ascii=False)}]',
                            'original': copy.deepcopy(raw), 'canonical': copy.deepcopy(value),
                            'rule': 'single_unknown_object_envelope'})
    return value, {'version': 1, 'raw_sha256': _digest(raw), 'canonical_sha256': _digest(value),
                   'expected_fields': sorted(fields), 'changes': changes,
                   'semantic_truth_verified': False}


def _witness_container(value, source_ids):
    if not isinstance(value, dict):
        return value
    if set(value) == {'source_id', 'quote'}:
        return [value]
    if (value and isinstance(source_ids, list) and all(isinstance(s, str) for s in source_ids)
            and set(value) <= set(source_ids)
            and all(isinstance(quote, str) for quote in value.values())):
        return [{'source_id': source_id, 'quote': quote} for source_id, quote in value.items()]
    return value  # Strict schema rejects ambiguous objects and unknown keys.


def witness_provenance(witness, sources):
    source_id, quote = witness.get('source_id'), witness.get('quote')
    if not isinstance(source_id, str) or not isinstance(quote, str):
        return {'exact_member': False, 'canonical_values': []}
    content = sources.get(source_id, '')
    start = content.find(quote) if quote else -1
    return {'source_id': source_id, 'original_quote': quote, 'exact_member': start >= 0,
            'source_span': [start, start + len(quote)] if start >= 0 else None,
            'source_content_sha256': hashlib.sha256(content.encode()).hexdigest(),
            'canonical_values': normalize_temporal(quote, source_id)}


def _quote_whitespace_rebinding(witness, sources):
    source_id, quote = witness.get('source_id'), witness.get('quote')
    if not isinstance(source_id, str) or not isinstance(quote, str) or not quote.strip():
        return None
    content = sources.get(source_id)
    if not isinstance(content, str) or quote in content:
        return None
    pieces = list(re.finditer(r'\s+|\S', content))
    folded = ''.join(' ' if part.group().isspace() else part.group() for part in pieces)
    needle = re.sub(r'\s+', ' ', quote)
    starts, cursor = [], 0
    while (start := folded.find(needle, cursor)) >= 0:
        starts.append(start)
        cursor = start + 1
    if not starts:
        return None
    spans = [(pieces[s].start(), pieces[s + len(needle) - 1].end()) for s in starts]
    if any(b > c for (_, b), (c, _) in zip(spans, spans[1:])):
        return None  # Overlapping text is not a uniquely bound excerpt.
    if len({content[a:b] for a, b in spans}) != 1:
        return None  # Multiple spellings must not be silently selected.
    begin, end = spans[0]
    canonical = content[begin:end]
    return {'rule': 'unique_source_unicode_whitespace_equivalence', 'source_id': source_id,
            'original': quote, 'canonical': canonical,
            'original_quote_sha256': hashlib.sha256(quote.encode()).hexdigest(),
            'canonical_quote_sha256': hashlib.sha256(canonical.encode()).hexdigest(),
            'source_content_sha256': hashlib.sha256(content.encode()).hexdigest(),
            'source_span': [begin, end], 'equivalent_source_spans': [list(s) for s in spans],
            'source_span_unit': 'unicode_codepoints',
            'source_utf8_span': [len(content[:begin].encode()), len(content[:end].encode())]}


def normalize_candidate_evidence(raw, enriched):
    """Return a canonical deep copy plus an audit; retain every unknown field."""
    value, changes, witnesses = copy.deepcopy(raw), [], []
    sources = {s['id']: s['content'] for s in enriched.get('sources', [])}
    candidates = value.get('candidates', []) if isinstance(value, dict) else []
    for ci, candidate in enumerate(candidates if isinstance(candidates, list) else []):
        claims = candidate.get('claims', []) if isinstance(candidate, dict) else []
        for index, claim in enumerate(claims if isinstance(claims, list) else []):
            if not isinstance(claim, dict):
                continue
            for field in ('source_witnesses', 'date_exception_witnesses'):
                if field not in claim:
                    continue
                path = f'$.candidates[{ci}].claims[{index}].{field}'
                before = claim[field]
                after = _witness_container(before, claim.get('source_ids'))
                if after != before:
                    changes.append({'path': path, 'original': copy.deepcopy(before),
                                    'canonical': copy.deepcopy(after), 'rule': 'unambiguous_witness_container'})
                claim[field] = after
                for wi, witness in enumerate(after if isinstance(after, list) else []):
                    if isinstance(witness, dict):
                        witness_path = f'{path}[{wi}]'
                        rebinding = _quote_whitespace_rebinding(witness, sources)
                        if rebinding:
                            witness['quote'] = rebinding['canonical']
                            changes.append({'path': f'{witness_path}.quote', **rebinding})
                        receipt = {'path': witness_path, **witness_provenance(witness, sources)}
                        if rebinding:
                            receipt['quote_normalization'] = rebinding
                        witnesses.append(receipt)
    return value, {'version': 1, 'raw_sha256': _digest(raw), 'canonical_sha256': _digest(value),
                   'changes': changes, 'witnesses': witnesses, 'semantic_truth_verified': False}


def _target_dates(value):
    if isinstance(value, dict):
        return set().union(set(), *(_target_dates(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(set(), *(_target_dates(v) for v in value))
    if not isinstance(value, str):
        return set()
    return {v['start'] for v in normalize_temporal(value, 'user_dates')
            if v['kind'] == 'date' and v['start'] == v['end']}


def _numbers_without_dates(text, values):
    masked = list(text)
    for value in values:
        if value['kind'] in {'date', 'time'}:
            provenance = value['provenance']
            spans = (provenance['component_spans'].values() if 'component_spans' in provenance
                     else [provenance['span']])
            for start, end in spans:
                masked[start:end] = ' ' * (end - start)
    return set(re.findall(r'(?<![\d.])\d+(?:[.,]\d+)*', ''.join(masked)))


def audit_temporal_evidence(claim, enriched):
    """Record canonical values and coverage, without deciding semantic support.

    Dates, amounts and applicability are judged by the unchanged claim critic.
    A matching number or a covering interval is representation evidence only.
    """
    sources = {s['id']: s['content'] for s in enriched.get('sources', [])}
    text = temporal_assertion_text(claim)
    asserted = normalize_temporal(text, 'claim')
    receipts = [witness_provenance(w.model_dump(), sources) for w in claim.source_witnesses]
    numbers = _numbers_without_dates(text, asserted)
    quoted_numbers = set().union(set(), *(
        _numbers_without_dates(r['original_quote'], r['canonical_values']) for r in receipts))
    dates = [witness_provenance(w.model_dump(), sources) for w in claim.date_exception_witnesses]
    exact_dates = [v for r in dates if r['exact_member'] for v in r['canonical_values']]
    return {'reasons': [], 'semantic_decision_owner': 'critic', 'numeric_tokens': sorted(numbers),
            'quoted_numeric_tokens': sorted(quoted_numbers),
            'temporal_evidence': {'asserted': asserted, 'source_witnesses': receipts,
                                  'date_exception_witnesses': dates,
                                  'target_date_coverage': {d: covers_date(exact_dates, d)
                                      for d in sorted(_target_dates(enriched.get('dates')))}}}
