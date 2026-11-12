"""Read-only trip media: named queries are not new travel facts or supplies.

Only Wikimedia endpoints are queried, through the existing bounded discovery
transport/cache. Google media never enters this service or the persistent cache.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

from .destination_discovery import DestinationDiscovery, WIKIPEDIA, _coords, _pages
from .store import StateError

MAPS_CONFIG_PATH = Path(__file__).resolve().parents[2] / 'data/private/google-maps-browser.json'
KINDS = {'city', 'station', 'attraction', 'landmark'}
IMAGE_FIELDS = ('image_url', 'image_source_url', 'image_author', 'image_license',
                'image_license_url', 'image_attribution', 'image_checked_at', 'image_is_generated')
MAX_MEDIA_BATCH = 8


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 240:
        return ''
    if re.search(r'[\x00-\x1f\x7f|]|https?://|www\.', value, re.I):
        return ''
    return value.strip()


def _normal(value):
    return re.sub(r'[^\w]', '', unicodedata.normalize('NFKC', value).casefold())


def _kind(record):
    kind = record.get('kind', '')
    if kind in KINDS:
        return kind
    if kind in {'hotel', 'restaurant', 'food', 'drink', 'stay', 'lodging', 'flight'}:
        return None
    if re.search(r'\b(?:station|bahnhof|gare)\b|[车車]站|駅', record.get('name', ''), re.I):
        return 'station'
    return 'attraction' if kind in {'place', 'experience', 'activity', 'see', 'do'} else None


def _safe_url(value, hosts=None):
    if (not isinstance(value, str) or len(value) > 4000
            or re.search(r'[\x00-\x20\x7f]', value)):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username
                and not parsed.password and parsed.port in (None, 443)
                and (hosts is None or parsed.hostname in hosts))
    except ValueError:
        return False


def _image_fields(record):
    author = record.get('image_author')
    if (not _safe_url(record.get('image_url'), {'upload.wikimedia.org', 'thumb.wikimedia.org'})
            or not _safe_url(record.get('image_source_url'), {'commons.wikimedia.org'})
            or not _name(record.get('image_license'))
            or not isinstance(author, str) or not author.strip() or len(author) > 500
            or record.get('image_is_generated') is True):
        return {}
    result = {key: record[key] for key in IMAGE_FIELDS if key in record}
    if not _safe_url(result.get('image_license_url'), {'creativecommons.org', 'www.creativecommons.org'}):
        result.pop('image_license_url', None)
    return result


def _contains_name(text, name):
    if not isinstance(text, str) or not name or len(name) < 2:
        return False
    # Latin names need a length floor and token boundaries; CJK place names
    # are meaningful at two characters (苏堤, 孤山), so the floor is Latin-only.
    if re.search(r'[A-Za-z]', name) and len(name) < 3:
        return False
    # Latin names need token boundaries; CJK titles commonly omit whitespace.
    expression = re.escape(name)
    if re.search(r'[A-Za-z]', name):
        expression = r'(?<!\w)' + expression + r'(?!\w)'
    return bool(re.search(expression, text, re.I))


def _context(trip):
    direction = trip.get('core_direction') or {}
    destinations = direction.get('destinations') or []
    aliases = {}
    for destination in destinations:
        if not isinstance(destination, dict) or not _name(destination.get('name')):
            continue
        label = _name(destination['name'])
        search = _name(destination.get('search_name')) or label
        aliases[_normal(label)] = aliases[_normal(search)] = (label, search)
    return direction, aliases


def media_manifest(trip):
    """No provider calls and no mutation; associations require IDs or named matches."""
    direction, aliases = _context(trip)
    plan = trip.get('plan') or trip.get('draft_plan') or {}
    entities = {}

    def add(record, supply_id=None):
        name, kind = _name(record.get('name')), _kind(record)
        if not name or not kind:
            return
        city = _name(record.get('city') or record.get('destination'))
        label, city_search = aliases.get(_normal(city), (city, city))
        search = _name(record.get('search_name')) or name
        if kind == 'city':
            label, city_search = aliases.get(_normal(name), (name, search))
        identity = ('city' if kind == 'city' else 'place') + ':' + _normal(city_search) + ':' + _normal(search)
        id_ = 'media-' + hashlib.sha256(identity.encode()).hexdigest()[:20]
        entity = entities.setdefault(id_, {'id': id_, 'name': name, 'search_name': search,
            'kind': kind, 'city': label, 'supply_ids': [], 'related_item_ids': []})
        if kind in {'station', 'landmark'}:
            entity['kind'] = kind
        if supply_id and supply_id not in entity['supply_ids']:
            entity['supply_ids'].append(supply_id)
        entity.update(_image_fields(record))
        coords = _coords(record.get('lat'), record.get('lng')) if supply_id else None
        if coords:
            entity.update(lat=coords[0], lng=coords[1])
        return entity

    for destination in direction.get('destinations') or []:
        if isinstance(destination, dict):
            add({**destination, 'kind': 'city'})
    for supply in plan.get('supplies') or []:
        if isinstance(supply, dict) and isinstance(supply.get('id'), str):
            add(supply, supply['id'])
    for hint in direction.get('place_hints') or []:
        if isinstance(hint, dict):
            add(hint)
    for station, item_id in _legacy_stations(plan, aliases):
        entity = add(station)
        if entity and item_id not in entity['related_item_ids']:
            entity['related_item_ids'].append(item_id)
    _associate_items(list(entities.values()), plan, aliases)
    return {'entities': list(entities.values()), 'revision': trip.get('revision')}


def _legacy_stations(plan, aliases):
    """Read explicit named station mentions in old plans, never infer transport.

    These remain lookup clues, not new supplies or verified station facts. Only
    a known destination's literal local + 站 or international + Station qualifies.
    """
    for day in plan.get('days') or []:
        for item in day.get('items') or []:
            if not isinstance(item.get('id'), str):
                continue
            for local, international in dict.fromkeys(aliases.values()):
                search = international + ' Station'
                local_name = local + '站'
                if _contains_name(item.get('title'), local_name):
                    name = local_name
                elif _contains_name(item.get('title'), search):
                    name = search
                else:
                    continue
                yield {'name': name, 'search_name': search, 'kind': 'station', 'city': local}, item['id']


def _associate_items(entities, plan, aliases):
    for day in plan.get('days') or []:
        city = aliases.get(_normal(_name(day.get('city'))), ('', ''))[0]
        for item in day.get('items') or []:
            if not isinstance(item.get('id'), str):
                continue
            for entity in entities:
                by_id = item.get('supply_id') in entity['supply_ids']
                by_name = (entity['kind'] != 'city' and city and city == entity['city']
                           and any(_contains_name(item.get('title'), name)
                                   for name in {entity['name'], entity['search_name']}))
                if (by_id or by_name) and item['id'] not in entity['related_item_ids']:
                    entity['related_item_ids'].append(item['id'])


def select_entities(manifest, body):
    if not isinstance(body, dict) or set(body) != {'entity_ids'}:
        raise StateError('BAD_INPUT', '图片请求仅接受entity_ids')
    ids = body.get('entity_ids')
    if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_MEDIA_BATCH
            or any(not isinstance(id_, str) or not re.fullmatch(r'media-[a-f0-9]{20}', id_) for id_ in ids)
            or len(set(ids)) != len(ids)):
        raise StateError('BAD_INPUT', '每次选择1—8个不重复的地点ID')
    known = {entity['id']: entity for entity in manifest['entities']}
    if any(id_ not in known for id_ in ids):
        raise StateError('BAD_INPUT', '地点ID不属于当前旅行版本')
    return [deepcopy(known[id_]) for id_ in ids]


def _page_query(**params):
    return {'action': 'query', 'format': 'json', 'formatversion': 2,
            'prop': 'coordinates|pageimages|extracts|description|pageprops',
            'ppprop': 'disambiguation', 'coprimary': 'primary', 'exintro': 1,
            'explaintext': 1, 'exchars': 1200, 'piprop': 'name', 'pilicense': 'free', **params}


def _exact_page(data, name):
    aliases = {_normal(name)}
    query = data.get('query', {})
    for _ in range(5):
        for link in query.get('normalized', []) + query.get('redirects', []):
            if _normal(link.get('from', '')) in aliases:
                aliases.add(_normal(link.get('to', '')))
    matches = [page for page in _pages(data) if _normal(page.get('title', '')) in aliases
               and page.get('pageid') and not page.get('missing')
               and 'disambiguation' not in (page.get('pageprops') or {})]
    return matches[0] if len(matches) == 1 else None


def _page_coords(page):
    coordinate = (page.get('coordinates') or [{}])[0]
    return _coords(coordinate.get('lat'), coordinate.get('lon'))


def _place_matches(page, entity, city_names, city_page):
    if not page.get('pageid') or 'disambiguation' in (page.get('pageprops') or {}):
        return False
    if entity['kind'] == 'city':
        return bool(_page_coords(page))
    # A station/landmark photo must not actually be the whole destination page.
    if _normal(page.get('title', '')) in {_normal(name) for name in city_names if name}:
        return False
    anchor = _coords(entity.get('lat'), entity.get('lng')) or _page_coords(city_page or {})
    point = _page_coords(page)
    if anchor and point:
        from packages.atomizer.common import haversine_km
        return haversine_km(*anchor, *point) <= 35
    description = ' '.join(str(page.get(key) or '') for key in ('title', 'extract', 'description'))
    return any(_contains_name(description, name) for name in city_names if name)


class PlaceMedia:
    def __init__(self, discovery=None):
        self.discovery = discovery or DestinationDiscovery()

    def enrich(self, trip, entities):
        output = deepcopy(entities)
        self._supply_images(trip, output)
        pending = [entity for entity in output if not entity.get('image_url')]
        self._named_images(trip, pending)
        for entity in output:
            entity['status'] = 'available' if entity.get('image_url') else 'unavailable'
            if entity['status'] == 'available':
                entity.pop('reason', None)
            else:
                entity.setdefault('reason', 'no_licensed_photo')
        return {'entities': output, 'revision': trip.get('revision')}

    def _supply_images(self, trip, entities):
        plan = trip.get('plan') or trip.get('draft_plan') or {}
        ids = {id_ for entity in entities if not entity.get('image_url') for id_ in entity['supply_ids']}
        supplies = [deepcopy(item) for item in plan.get('supplies', []) if item.get('id') in ids]
        for supply in supplies:
            if not _image_fields(supply):
                for key in IMAGE_FIELDS:
                    supply.pop(key, None)
        if not supplies:
            return
        enriched = {item['id']: item for item in self.discovery.enrich_images(supplies)}
        for entity in entities:
            for id_ in entity['supply_ids']:
                entity.update(_image_fields(enriched.get(id_, {})))

    def _named_images(self, trip, entities):
        _, aliases = _context(trip)
        jobs, cities = [], {}
        for entity in entities:
            jobs.append((entity['id'], WIKIPEDIA, _page_query(titles=entity['search_name'], redirects=1)))
            city = aliases.get(_normal(entity['city']), (entity['city'], entity['city']))[1]
            if city and entity['kind'] != 'city':
                cities[city] = ('city:' + city, WIKIPEDIA, _page_query(titles=city, redirects=1))
        evidence = []
        data = self.discovery._batch(jobs + list(cities.values()), evidence) if jobs else {}
        pages, search_jobs = {}, []
        for entity in entities:
            city_search = aliases.get(_normal(entity['city']), (entity['city'], entity['city']))[1]
            city_page = _exact_page(data.get('city:' + city_search, {}), city_search)
            page = _exact_page(data.get(entity['id'], {}), entity['search_name'])
            context = (entity['city'], city_search)
            if page and _place_matches(page, entity, context, city_page):
                pages[entity['id']] = page
            else:
                query = '"' + entity['search_name'].replace('"', ' ') + '" ' + city_search
                search_jobs.append((entity['id'], WIKIPEDIA, _page_query(
                    generator='search', gsrsearch=query[:500], gsrlimit=5)))
        searches = self.discovery._batch(search_jobs, evidence) if search_jobs else {}
        self._select_searches(entities, searches, data, aliases, pages)
        self._attach(entities, pages, evidence)

    @staticmethod
    def _select_searches(entities, searches, exact, aliases, pages):
        for entity in entities:
            if entity['id'] in pages:
                continue
            city_search = aliases.get(_normal(entity['city']), (entity['city'], entity['city']))[1]
            city_page = _exact_page(exact.get('city:' + city_search, {}), city_search)
            names = {_normal(entity['name']), _normal(entity['search_name'])}
            matches = []
            for page in _pages(searches.get(entity['id'], {})):
                title = re.sub(r'\s*\([^)]*\)\s*$', '', page.get('title', ''))
                if (_normal(title) in names and _place_matches(
                        page, entity, (entity['city'], city_search), city_page)):
                    matches.append(page)
            if len(matches) == 1:
                pages[entity['id']] = matches[0]
            else:
                entity['reason'] = 'ambiguous_place' if len(matches) > 1 else 'place_not_matched'
                if not searches.get(entity['id']) and not exact.get(entity['id']):
                    entity['reason'] = 'provider_unavailable'

    def _attach(self, entities, pages, evidence):
        records = []
        for entity in entities:
            page = pages.get(entity['id'])
            if not page:
                continue
            record = {'id': entity['id'], '_image_file': page.get('pageimage')}
            records.append(record)
            coords = _page_coords(page)
            if coords and 'lat' not in entity:
                entity.update(lat=coords[0], lng=coords[1])
        self.discovery._enrich_image_files(records, evidence, datetime.now(timezone.utc).isoformat())
        by_id = {record['id']: record for record in records}
        for entity in entities:
            entity.update(_image_fields(by_id.get(entity['id'], {})))


def maps_config(environ=None, config_path=None):
    """Explicitly public browser credentials only; never inspect generic API keys."""
    environ = os.environ if environ is None else environ
    path = MAPS_CONFIG_PATH if config_path is None else Path(config_path)
    private = {}
    try:
        if path.stat().st_size <= 8192:
            value = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(value, dict):
                private = value
    except (OSError, ValueError, TypeError):
        pass
    key = environ.get('GOOGLE_MAPS_BROWSER_KEY', private.get('browser_key', ''))
    map_id = environ.get('GOOGLE_MAPS_MAP_ID', private.get('map_id', ''))
    enabled = environ.get('GOOGLE_MAPS_PLACES_ENABLED', private.get('places_enabled', False))
    key = key.strip() if isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9_-]{8,200}', key.strip()) else ''
    map_id = map_id.strip() if isinstance(map_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', map_id.strip()) else ''
    enabled = enabled is True or isinstance(enabled, str) and enabled.lower() in {'true', '1'}
    return {'configured': bool(key), 'browser_key': key, 'map_id': map_id,
            'places_enabled': bool(key and enabled), 'provider': 'google'}
