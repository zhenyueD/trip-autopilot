"""Identity-only quote shapes rebound to unique original capture spans.

Only Unicode whitespace runs and ASCII/U+2019 apostrophes between alphabetic
characters are equivalent. The unchanged schema and binding validator decide
acceptance; this adapter neither repairs content nor establishes semantic truth.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _text_digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _quote_pattern(text):
    pattern = []
    for piece in re.finditer(r'\s+|\S', text):
        char, start, end = piece.group(), piece.start(), piece.end()
        if char.isspace():
            # Bind an entire original whitespace run, never a partial run.
            pattern.append(r'(?<!\s)\s+(?!\s)')
        elif (char in {"'", '\u2019'} and start > 0 and end < len(text)
              and text[start - 1].isalpha() and text[end].isalpha()):
            pattern.append("['\u2019]")
        else:
            pattern.append(re.escape(char))
    return ''.join(pattern)


def _rebind(quote, capture):
    content = capture['text']
    if not quote.strip() or quote in content:
        return None
    matches = re.finditer(f'(?=({_quote_pattern(quote)}))', content)
    match = next(matches, None)
    if match is None or next(matches, None) is not None:
        return None  # A second overlapping match is also ambiguous.
    begin, end = match.span(1)
    canonical = content[begin:end]
    return {'rule': 'unique_capture_whitespace_intraword_ascii_u2019_apostrophe_equivalence',
            'capture_id': capture['id'], 'original': quote, 'canonical': canonical,
            'original_quote_sha256': _text_digest(quote),
            'canonical_quote_sha256': _text_digest(canonical),
            'source_capture_sha256': _digest(capture),
            'source_content_sha256': _text_digest(content),
            'source_span': [begin, end], 'source_span_unit': 'unicode_codepoints',
            'source_utf8_span': [len(content[:begin].encode('utf-8')),
                                 len(content[:end].encode('utf-8'))],
            'source_utf8_span_unit': 'bytes'}


def normalize_identity_quotes(raw, captures):
    """Copy raw output; change eligible quote strings only, never supplied captures."""
    value, changes = copy.deepcopy(raw), []
    sources = {capture['id']: capture for capture in captures}
    assessments = value.get('assessments', []) if isinstance(value, dict) else []
    for ai, assessment in enumerate(assessments if isinstance(assessments, list) else []):
        if not isinstance(assessment, dict):
            continue
        source_id = assessment.get('source_id')
        primary = sources.get(source_id) if isinstance(source_id, str) else None
        if primary is None or primary['role'] != 'primary':
            continue
        quotes = assessment.get('quotes', [])
        for qi, witness in enumerate(quotes if isinstance(quotes, list) else []):
            if not isinstance(witness, dict):
                continue
            capture_id, quote = witness.get('capture_id'), witness.get('quote')
            if not isinstance(capture_id, str) or not isinstance(quote, str):
                continue
            capture = sources.get(capture_id)
            if capture is None or (capture['role'] == 'primary' and capture_id != source_id):
                continue
            change = _rebind(quote, capture)
            if change:
                witness['quote'] = change['canonical']
                changes.append({'path': f'$.assessments[{ai}].quotes[{qi}].quote',
                                'source_id': source_id, **change})
    return value, {'version': 1, 'raw_sha256': _digest(raw), 'canonical_sha256': _digest(value),
                   'changes': changes, 'semantic_truth_verified': False}
