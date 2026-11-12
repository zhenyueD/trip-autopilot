"""Source-bound temporal values. Parsing proves representation, never factual truth.

No inferred year, timezone, exception clearance, or cross-source composition.
Unknown forms remain absent and cannot establish a target-date relationship.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date

MONTHS = {name: index for index, name in enumerate(
    ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'), 1)}
MONTH = r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
JOIN = r'\s*(?:to|through|until|至|到|[–—-])\s*'
ISO = r'\d{4}-\d{2}-\d{2}'
YEAR_FIRST = rf'(?:{ISO}|\d{{4}}/\d{{1,2}}/\d{{1,2}})'


def _calendar(year, month, day):
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except (ValueError, TypeError):
        return None


def _value(text, source_id, span, kind, start, end=None):
    return {'kind': kind, 'start': start, 'end': end or start,
            'provenance': {'source_id': source_id, 'span': list(span),
                           'raw': text[span[0]:span[1]],
                           'text_sha256': hashlib.sha256(text.encode()).hexdigest()}}


def _date_patterns():
    month = lambda v: MONTHS[v.lower()[:3]]
    return [
        (rf'(?<![A-Za-z0-9_/-])({YEAR_FIRST}){JOIN}({YEAR_FIRST})(?![A-Za-z0-9_/-])',
         lambda m: (_calendar(*re.split(r'[-/]', m[1])), _calendar(*re.split(r'[-/]', m[2])))),
        (rf'(?<![A-Za-z0-9_/-])({ISO}){JOIN}(\d{{2}})-(\d{{2}})(?![A-Za-z0-9_/-])',
         lambda m: (_calendar(*m[1].split('-')), _calendar(m[1][:4], m[2], m[3]))),
        (rf'(?<![A-Za-z0-9])(\d{{1,2}})\s+({MONTH})\s+(\d{{4}}){JOIN}(\d{{1,2}})\s+({MONTH})\s+(\d{{4}})(?![A-Za-z0-9])',
         lambda m: (_calendar(m[3], month(m[2]), m[1]), _calendar(m[6], month(m[5]), m[4]))),
        (rf'(?<![A-Za-z0-9])(\d{{1,2}})\s+({MONTH}){JOIN}(\d{{1,2}})\s+({MONTH})\s+(\d{{4}})(?![A-Za-z0-9])',
         lambda m: (_calendar(m[5], month(m[2]), m[1]), _calendar(m[5], month(m[4]), m[3]))),
        (rf'(?<![A-Za-z0-9])(\d{{1,2}}){JOIN}(\d{{1,2}})\s+({MONTH})\s+(\d{{4}})(?![A-Za-z0-9])',
         lambda m: (_calendar(m[4], month(m[3]), m[1]), _calendar(m[4], month(m[3]), m[2]))),
        (rf'(?<![A-Za-z0-9_/-])({YEAR_FIRST})(?![A-Za-z0-9_/-])',
         lambda m: (_calendar(*re.split(r'[-/]', m[1])),)),
        (rf'(?<![A-Za-z0-9])(\d{{1,2}})\s+({MONTH})\s+(\d{{4}})(?![A-Za-z0-9])',
         lambda m: (_calendar(m[3], month(m[2]), m[1]),)),
        (rf'(?<![A-Za-z0-9])({MONTH})\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(\d{{4}})(?![A-Za-z0-9])',
         lambda m: (_calendar(m[3], month(m[1]), m[2]),)),
        (r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日',
         lambda m: (_calendar(m[1], m[2], m[3]),)),
    ]


def _dates(text, source_id):
    values, consumed = [], []
    for pattern, convert in _date_patterns():
        for match in re.finditer(pattern, text, re.I):
            if any(a <= match.start() and match.end() <= b for a, b in consumed):
                continue
            consumed.append(match.span())  # Invalid ranges must not leak endpoint dates.
            endpoints = convert(match)
            start, end = endpoints[0], endpoints[-1]
            if start and end and end >= start:
                values.append(_value(text, source_id, match.span(), 'date', start, end))
    return values


def _object_member_spans(text, start, end, decoder):
    """Find direct JSON value spans; reject duplicate keys instead of guessing."""
    spans, cursor = {}, start + 1
    while cursor < end - 1:
        while cursor < end and text[cursor].isspace():
            cursor += 1
        if text[cursor] == '}':
            break
        key, cursor = decoder.raw_decode(text, cursor)
        while text[cursor].isspace():
            cursor += 1
        cursor += 1  # The full object has already validated the colon.
        while text[cursor].isspace():
            cursor += 1
        value_start = cursor
        _, cursor = decoder.raw_decode(text, cursor)
        if key in spans:
            return None
        spans[key] = [value_start, cursor]
        while text[cursor].isspace():
            cursor += 1
        if text[cursor] == ',':
            cursor += 1
    return spans


def _object_value(text, source_id, span, kind, value, fields, member_spans):
    normalized = _value(text, source_id, span, kind, value)
    normalized['provenance']['component_spans'] = {key: member_spans[key] for key in fields}
    return normalized


def _objects(text, source_id):
    values, decoder = [], json.JSONDecoder()
    for match in re.finditer(r'\{', text):
        try:
            obj, length = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        span = (match.start(), match.start() + length)
        member_spans = _object_member_spans(text, *span, decoder)
        if member_spans is None:
            continue
        if all(type(obj.get(k)) is int for k in ('year', 'month', 'day')):
            value = _calendar(obj['year'], obj['month'], obj['day'])
            if value:
                values.append(_object_value(text, source_id, span, 'date', value,
                                            ('year', 'month', 'day'), member_spans))
        if (type(obj.get('hour')) is int and type(obj.get('minute')) is int
                and 0 <= obj['hour'] < 24 and 0 <= obj['minute'] < 60):
            values.append(_object_value(text, source_id, span, 'time',
                                        f"{obj['hour']:02}:{obj['minute']:02}",
                                        ('hour', 'minute'), member_spans))
    return values


def _times(text, source_id):
    values = []
    pattern = r'(?<![A-Za-z0-9_:])(\d{1,2})(?::(\d{2})|(\d{2})(?=\s*[ap]m))?\s*([ap]m)?(?![A-Za-z0-9_:])'
    for match in re.finditer(pattern, text, re.I):
        hour, minute = int(match[1]), int(match[2] or match[3] or 0)
        suffix = (match[4] or '').lower()
        if not (match[2] or suffix) or minute >= 60:
            continue
        if suffix:
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if suffix == 'pm' else 0)
        if hour > 24 or (hour == 24 and minute):
            continue
        values.append(_value(text, source_id, match.span(), 'time', f'{hour:02}:{minute:02}'))
    return values


def normalize_temporal(text, source_id):
    """Canonical dates/ranges, clock values and exception cues with exact provenance."""
    values = _dates(text, source_id) + _objects(text, source_id) + _times(text, source_id)
    for pattern, kind in ((r'\bclosed\b|关闭|闭馆|停业', 'closed'),
                          (r'\bdaily\b|每日|每天', 'recurring'),
                          (r'目标日期例外未核验|当日例外未核验|例外待核实', 'exception_unknown')):
        for match in re.finditer(pattern, text, re.I):
            values.append(_value(text, source_id, match.span(), 'exception', kind))
    return sorted(values, key=lambda item: (item['provenance']['span'], item['kind']))


def covers_date(values, target):
    return any(v['kind'] == 'date' and v['start'] <= target <= v['end'] for v in values)


def temporal_assertion_text(claim):
    """Remove only the explicit regular-schedule exception disclaimer from assertions."""
    text = claim.text
    if claim.temporal_scope == 'regular_schedule':
        text = re.sub(r'\d{4}-\d{2}-\d{2}(?:当日)?例外未核验', '', text)
    return text


def validate_temporal_scope(claim, target_dates):
    """Reject contradictory labels before the unchanged downstream evidence gates."""
    text = temporal_assertion_text(claim)
    asserted = normalize_temporal(text, 'claim')
    dated = any(v['kind'] == 'date' for v in asserted)
    clocked = any(v['kind'] == 'time' for v in asserted)
    if claim.temporal_scope != 'target_date' and dated:
        raise ValueError('Temporal label evasion: dated assertion requires target_date witnesses')
    if claim.temporal_scope == 'not_applicable' and clocked:
        raise ValueError('Temporal label evasion: clock assertion requires a temporal scope')
    if claim.temporal_scope != 'target_date' and claim.date_exception_witnesses:
        raise ValueError('Temporal label evasion: dated witnesses require target_date')
    if claim.temporal_scope == 'target_date':
        values = [v for w in claim.date_exception_witnesses
                  for v in normalize_temporal(w.quote, w.source_id)]
        if not target_dates or not claim.date_exception_witnesses:
            raise ValueError('Target-date assertions need dated exception evidence')
        if not any(covers_date(values, target) for target in target_dates):
            raise ValueError('Date-exception witness must contain a supplied target date or equivalent interval')
