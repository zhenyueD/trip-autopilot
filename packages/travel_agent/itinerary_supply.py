"""Import accepted supply files into the existing itinerary seed/source schema.

Extracted from the C5 input freezer: the eight-column Markdown table and frozen
seed JSON list remain unchanged. Importing records their producer's assertions;
it is not a new source fetch or independent factual verification. No monetary
value is parsed from claims, quotes, or destination metadata.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TABLE_HEADER = ('ID', '主张', '品类', '官方来源', '逐字引文', '核验日期', '新鲜期', '等级')
OVERLAY_FIELDS = ('source_name', 'verification_provenance', 'offers',
                  'offer_transcription_authority', 'verification_status',
                  'independent_cross_check')


def _destination(value):
    if not isinstance(value, dict):
        raise ValueError('destination must contain city, currency and timezone')
    if any(not isinstance(value.get(key), str) or not value[key].strip()
           for key in ('city', 'currency', 'timezone')):
        raise ValueError('destination city, currency and timezone must be nonempty strings')
    result = {key: value[key].strip() for key in ('city', 'currency', 'timezone')}
    if not re.fullmatch(r'[A-Z]{3}', result['currency']):
        raise ValueError('destination currency must be a three-letter uppercase code')
    try:
        zone = ZoneInfo(result['timezone'])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError('destination timezone must be a valid IANA timezone') from exc
    return result, zone


def _day(value, zone):
    if isinstance(value, datetime):
        return value.astimezone(zone).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not re.match(r'\d{4}-\d{2}-\d{2}(?:$|[T ])', value):
        return None
    try:
        return _day(datetime.fromisoformat(value.replace('Z', '+00:00')), zone)
    except ValueError:
        return None


def _fresh_until(text, checked, zone):
    """Interpret only the existing freshness column, never the claim or quote."""
    if not isinstance(text, str):
        return ''
    value = text.strip()
    explicit = re.fullmatch(r'(?:事件日|事件期至|至)?\s*(\d{4}-\d{2}-\d{2})', value)
    if explicit:
        parsed = _day(explicit[1], zone)
        return parsed.isoformat() if parsed else ''
    duration = re.fullmatch(r'(\d+)\s*(?:天|days?)', value, re.IGNORECASE)
    start = _day(checked, zone)
    if duration and start:
        try:
            return (start + timedelta(days=int(duration[1]))).isoformat()
        except OverflowError:
            pass
    return ''


def _cells(line):
    return [part.strip().replace(r'\|', '|')
            for part in re.split(r'(?<!\\)\|', line.strip().strip('|'))]


def _markdown_rows(text, zone):
    rows, in_table = [], False
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip().startswith('|'):
            in_table = False
            continue
        cells = _cells(line)
        if tuple(cells) == TABLE_HEADER:
            in_table = True
            continue
        if not in_table or all(re.fullmatch(r':?-+:?', cell) for cell in cells):
            continue
        if len(cells) != len(TABLE_HEADER):
            raise ValueError(f'supply table line {number} must contain eight columns')
        key, claim, category, url, quote, checked, fresh, grade = cells
        rows.append(dict(id=key, claim=claim, category=category, source_url=url,
            source_name=urlsplit(url).hostname or '', quote=quote, checked_at=checked,
            fresh_until=_fresh_until(fresh, checked, zone), freshness_text=fresh,
            grade=grade, verification_status='verified',
            verification_provenance='Imported accepted supply library; original verification belongs to its producer',
            independent_cross_check='not_recorded_in_seed', imported_from=dict(line=number)))
    return rows


def _index(rows, label):
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(label + ' must be a list of objects')
    ids = [row.get('id') for row in rows]
    if any(not isinstance(key, str) or not key.strip() for key in ids) or len(set(ids)) != len(ids):
        raise ValueError(label + ' IDs must be nonempty and unique')
    return dict(zip(ids, rows))


def _structured_input(value):
    if value is None:
        return {}, None
    provenance = None
    if isinstance(value, (str, Path)):
        path = Path(value).resolve()
        raw = path.read_bytes()
        value = json.loads(raw)
        provenance = dict(path=str(path), file_sha256=hashlib.sha256(raw).hexdigest())
    else:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')
        provenance = dict(content_sha256=hashlib.sha256(raw).hexdigest())
    return _index(value, 'structured seeds'), provenance


def _overlay(row, prior):
    if not prior:
        return row
    for key in ('claim', 'source_url', 'quote', 'checked_at', 'grade'):
        if row.get(key) != prior.get(key):
            raise ValueError(f'structured seed {row["id"]} has a different {key}')
    result = dict(row)
    for key in OVERLAY_FIELDS:
        if key in prior:
            result[key] = copy.deepcopy(prior[key])
    if not result.get('fresh_until') and prior.get('fresh_until'):
        result['fresh_until'] = prior['fresh_until']
    return result


def _freshness(row, as_of, zone):
    checked, end = _day(row.get('checked_at'), zone), _day(row.get('fresh_until'), zone)
    if not checked or not end or checked > end:
        return 'unknown'
    if checked > as_of:
        return 'future_observation'
    return 'current' if end >= as_of else 'expired_event_audit_only'


def _seeds(rows, overlays, path, file_hash, as_of, zone):
    index = _index(rows, 'supply seeds')
    if not index:
        raise ValueError('supply file contains no atoms in the existing schema')
    missing = overlays.keys() - index.keys()
    if missing:
        raise ValueError('structured seed IDs missing from supply file: ' + ', '.join(sorted(missing)))
    result = []
    for index, row in enumerate(rows):
        if any(not isinstance(row.get(key), str) or not row[key].strip()
               for key in ('claim', 'source_url', 'quote', 'checked_at', 'grade')):
            raise ValueError(f'supply seed {row["id"]} lacks required source fields')
        seed = _overlay(copy.deepcopy(row), overlays.get(row['id']))
        origin = seed.get('imported_from') if isinstance(seed.get('imported_from'), dict) else {}
        if path.suffix.lower() == '.json':
            origin = dict(json_index=index, producer_import=origin)
        seed['imported_from'] = dict(origin, path=str(path), file_sha256=file_hash)
        seed['as_of'] = as_of.isoformat()
        seed['freshness_status'] = _freshness(seed, as_of, zone)
        seed.setdefault('fresh_until', '')
        result.append(seed)
    return result


def _source(seed):
    verified = seed.get('verification_status') == 'verified'
    return dict(id=seed['id'], title=seed['claim'], url=seed['source_url'], content=seed['quote'],
        source_origin='accepted_verified_supply_library' if verified else 'unverified_supply_input',
        original_read_status='official_verbatim_verified_by_seed_producer' if verified else 'not_recorded',
        factual_status='official_supported_seed' if verified else 'unverified_seed',
        checked_at=seed['checked_at'],
        fresh_until=seed['fresh_until'], freshness_status=seed['freshness_status'],
        applicable_scope=seed.get('freshness_text', ''), grade=seed['grade'],
        independence_status='同机构多页非独立来源；本轮未新增独立交叉核验',
        usable_as_verified_supply=verified and seed['freshness_status'] == 'current',
        source_limit='过期或新鲜期未知原子仅留审计，不能当当前开放/关闭承诺',
        import_provenance=copy.deepcopy(seed['imported_from']))


def load_itinerary_supply(path, *, destination, as_of, structured_seeds=None):
    """Load accepted Markdown or the existing JSON seed list without new research.

    ``destination`` supplies city/currency/timezone; aware timestamps are converted
    to its local date. ``structured_seeds`` optionally points to a prior approved
    JSON seed list (or supplies that list directly). Only matching source identity
    can carry its offers and existing metadata forward. Unknown freshness stays
    unknown. Destination currency never overwrites an offer's own currency.
    """
    metadata, zone = _destination(destination)
    local_day = _day(as_of, zone)
    if local_day is None:
        raise ValueError('as_of must be a valid ISO date or timestamp')
    path = Path(path).resolve()
    raw = path.read_bytes()
    file_hash = hashlib.sha256(raw).hexdigest()
    text = raw.decode('utf-8-sig')
    rows = json.loads(text) if path.suffix.lower() == '.json' else _markdown_rows(text, zone)
    overlays, overlay_provenance = _structured_input(structured_seeds)
    seeds = _seeds(rows, overlays, path, file_hash, local_day, zone)
    evidence = [dict(seed_id=s['id'], claim=s['claim'], quote=s['quote'],
        source_url=s['source_url'], checked_at=s['checked_at'], fresh_until=s['fresh_until'],
        factual_status=('accepted_official_seed' if s.get('verification_status') == 'verified'
                        else 'unverified_seed'), freshness_status=s['freshness_status']) for s in seeds]
    provenance = dict(supply_file=str(path), supply_file_sha256=file_hash,
        verified_supply_count=sum(s.get('verification_status') == 'verified' for s in seeds),
        current_fresh_supply_count=sum(s['freshness_status'] == 'current' for s in seeds),
        supply_count=len(seeds), new_external_verification=False)
    if overlay_provenance:
        provenance['structured_seed_input'] = overlay_provenance
    return dict(seeds=seeds, supply_seeds=seeds, sources=[_source(s) for s in seeds],
                supply_evidence=evidence, destination=metadata, price_as_of=local_day.isoformat(),
                provenance=provenance)
