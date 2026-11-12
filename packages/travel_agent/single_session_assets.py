"""Lossless source metadata exposure and exact-identity ledger asset projection.

No network, downloads, entity inference, or authority promotion occurs here.
"""
import base64
from copy import deepcopy
import json
import math
from urllib.parse import urlsplit


def _url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme in ('http', 'https') and bool(parsed.hostname) and not parsed.username
    except ValueError:
        return False


def source_text_with_assets(capture):
    """Keep original text plus publisher-declared metadata from the same capture.

    Metadata is source material, not validated destination imagery. Never expose
    metadata from a rejected capture or substitute generated descriptions.
    """
    text = capture.get('text', '')
    if capture.get('status') != 'read':
        return text
    try:
        payload = json.loads(base64.b64decode(capture['raw_response_base64']))
        data = payload.get('data', payload)
        if data.get('url') != capture.get('url') or data.get('text') != text:
            return text
        metadata = data.get('metadata', {})
        selected = {k: v for k, v in metadata.items() if k in (
            'og:title', 'og:description', 'og:image', 'og:image:url',
            'og:image:secure_url', 'og:video', 'og:video:url',
            'twitter:image', 'twitter:image:src', 'twitter:title') and isinstance(v, str)}
    except (KeyError, ValueError, TypeError, AttributeError):
        return text
    if not any('image' in k or 'video' in k for k in selected):
        return text
    return text + '\n\n原页媒体元数据（候选，须判断实体及点位，不代表授权或已核实画面）：\n' + json.dumps(selected, ensure_ascii=False)


def _records(ledger):
    for evidence_id, record in ledger.get('evidence', {}).items():
        try:
            asset = json.loads(record.get('claim', '')).get('destination_asset')
        except (ValueError, TypeError, AttributeError):
            continue
        if isinstance(asset, dict) and isinstance(asset.get('place'), str):
            yield evidence_id, record, asset


def _media(item, record, evidence_id, video=False):
    if not isinstance(item, dict) or not all(_url(item.get(k)) for k in ('url', 'source_url')):
        return None
    if item['source_url'] != record.get('url'):
        return None
    if video and (item.get('source_type') not in ('official', 'scenic_official') or not _url(item.get('poster_url'))
                  or not item.get('authority_basis')):
        return None
    return {**deepcopy(item), 'evidence_id': evidence_id}


def _coordinate(value):
    if not isinstance(value, dict) or not _url(value.get('source_url')):
        return False
    for field, limit in [('latitude', 90), ('longitude', 180)]:
        x = value.get(field)
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or abs(x) > limit:
            return False
    return value.get('crs') in ('WGS84', 'WGS-84', 'GCJ02', 'GCJ-02', 'BD09', 'BD-09')


def _merge_owner(owner, records, *, media=False):
    name = owner.get('name')
    coordinates = []
    for evidence_id, record, asset in records:
        # A point scoped to an entrance never fills the enclosing scenic area.
        target = asset.get('point') or asset['place']
        if target == name and _coordinate(asset.get('coordinates')):
            c = asset['coordinates']
            if c['source_url'] == record.get('url'):
                coordinates.append({**deepcopy(c), 'evidence_id': evidence_id})
        if not media or asset['place'] != name:
            continue
        for key in ('images', 'videos'):
            for item in asset.get(key, []) if isinstance(asset.get(key, []), list) else []:
                candidate = _media(item, record, evidence_id, video=key == 'videos')
                if candidate and not any(x.get('url') == candidate['url'] for x in owner.get(key, [])):
                    owner.setdefault(key, []).append(candidate)
    unique = {(c['latitude'], c['longitude'], c['crs'].replace('-', '')) for c in coordinates}
    if not owner.get('coordinates') and len(unique) == 1:
        owner['coordinates'] = coordinates[0]


def merge_assets(detail, ledger):
    """Return a deep copy with ledger media/coordinates; never alter source state.

    Existing explicit values win. Conflicting ledger coordinates remain absent.
    Images match exact place; coordinates match exact point (or place if empty).
    """
    if detail is None:
        return None
    result, records = deepcopy(detail), list(_records(ledger))
    for day in result.get('days', []):
        for stop in day.get('stops', []):
            _merge_owner(stop, records)
        for spot in day.get('attractions', []):
            _merge_owner(spot, records, media=True)
            for stop in spot.get('stops', []):
                _merge_owner(stop, records)
    return result
