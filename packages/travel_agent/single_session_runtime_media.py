"""Opt-in, bounded media lookup for already named handbook places.

No calls occur on import or construction. Images are source candidates, not current
conditions evidence. Each card/day is independently degradable. No disk cache.
"""
from base64 import b64encode
from copy import deepcopy
from datetime import datetime, timezone
import html
import json
import math
import re
import threading
import time
import urllib.error
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .place_media import _contains_name, _name, _normal, _safe_url

COMMONS = 'https://commons.wikimedia.org/w/api.php'
GEOCODER = 'https://nominatim.openstreetmap.org/search'
IMAGE_HOSTS = {'upload.wikimedia.org', 'thumb.wikimedia.org', 'tile.openstreetmap.org'}
_GEOCODE_LOCK = threading.Lock()
_GEOCODE_LAST = 0.0


class MediaError(ValueError):
    def __init__(self, rule, message):
        super().__init__(message)
        self.rule = rule


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MediaError('provider_redirect', 'Provider redirect refused')


def _fetch_json(endpoint, params):
    global _GEOCODE_LAST
    if endpoint not in {COMMONS, GEOCODER}:
        raise MediaError('provider_host', 'Provider endpoint not allowed')
    # Nominatim's public service allows at most one request per second.
    if endpoint == GEOCODER:
        with _GEOCODE_LOCK:
            time.sleep(max(0, 1.05 - (time.monotonic() - _GEOCODE_LAST)))
            _GEOCODE_LAST = time.monotonic()
    request = Request(endpoint + '?' + urlencode(params), headers={
        'User-Agent': 'TravelHandbookPresentation/1.0 (named-place media lookup)',
        'Accept': 'application/json', 'Accept-Encoding': 'identity'})
    last_error = None
    for _attempt in range(2):
        try:
            with build_opener(_NoRedirect()).open(request, timeout=8) as response:
                body = response.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024:
                    raise MediaError('provider_response_limit', 'JSON response exceeds 1 MiB')
                return json.loads(body)
        except (TimeoutError, urllib.error.URLError) as error:
            last_error = error
    raise last_error


def _fetch_image(url):
    # Unlike the PDF path's DNS-pinned transport, media downloads must honor
    # the process proxy: Wikimedia hosts time out direct from this network.
    # The host allow-list is enforced by the caller before this runs.
    # One retry: the proxy route occasionally drops a single TLS handshake.
    request = Request(url, headers={
        'User-Agent': 'TravelHandbookPresentation/1.0 (named-place media lookup)',
        'Accept': 'image/*', 'Accept-Encoding': 'identity'})
    last_error = None
    for _attempt in range(2):
        try:
            with build_opener(ProxyHandler()).open(request, timeout=20) as response:
                mime = response.headers.get_content_type()
                body = response.read(8 * 1024 * 1024 + 1)
                if (mime not in {'image/jpeg', 'image/png', 'image/webp'} or not body
                        or len(body) > 8 * 1024 * 1024):
                    raise MediaError('image_response', 'Missing or unsupported raster image')
                return mime, body
        except MediaError:
            raise
        except (TimeoutError, urllib.error.URLError) as error:
            last_error = error
            time.sleep(2)
    raise last_error


def _base_name(name):
    """Strip a trailing parenthetical qualifier: '三潭印月（小瀛洲）' -> '三潭印月'."""
    base = re.sub(r'\s*[（(][^（）()]*[）)]\s*$', '', name).strip()
    return base or name


def _haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2)
         * math.sin(delta_lambda / 2) ** 2)
    return 2 * radius * math.asin(math.sqrt(a))


def _plain(value):
    return html.unescape(re.sub(r'<[^>]*>', '', str(value or ''))).strip()[:1500]


def _failure(path, stage, error):
    return {'field_path': path, 'rule': getattr(error, 'rule', stage + '_unavailable'),
            'stage': stage, 'message': str(error)[:240] or type(error).__name__}


def _point(lat, lon):
    lat, lon = float(lat), float(lon)
    if not (math.isfinite(lat) and math.isfinite(lon) and -85 <= lat <= 85 and -180 <= lon <= 180):
        raise MediaError('coordinate_range', 'Coordinates outside map bounds')
    return lat, lon


def _project(lat, lon, zoom):
    scale = 256 * 2 ** zoom
    radians = math.radians(lat)
    return ((lon + 180) / 360 * scale,
            (1 - math.asinh(math.tan(radians)) / math.pi) / 2 * scale)


class RuntimeMediaProvider:
    """fetch(endpoint, params) returns JSON; image_fetch(url) returns mime, bytes."""
    def __init__(self, fetch=None, image_fetch=None):
        self.fetch = fetch or _fetch_json
        self.image_fetch = image_fetch or _fetch_image

    def _image_ready(self, url):
        if not _safe_url(url, IMAGE_HOSTS):
            raise MediaError('image_host', 'Image must use an approved HTTPS media host')
        mime, body = self.image_fetch(url)
        if mime not in {'image/jpeg', 'image/png', 'image/webp'} or not body or len(body) > 8 * 1024 * 1024:
            raise MediaError('image_response', 'Missing or unsupported raster image')
        return mime, body

    def _photo(self, name):
        attempts = [name]
        base = _base_name(name)
        if base != name:
            attempts.append(base)
        last_error = None
        for attempt in attempts:
            try:
                return self._photo_attempt(attempt, name)
            except MediaError as error:
                if error.rule != 'photo_no_named_match':
                    raise
                last_error = error
        raise last_error

    def _photo_attempt(self, attempt, original_name):
        payload = self.fetch(COMMONS, {'action': 'query', 'format': 'json', 'formatversion': 2,
            'generator': 'search', 'gsrsearch': '"' + attempt + '" filetype:bitmap',
            'gsrnamespace': 6, 'gsrlimit': 5, 'prop': 'imageinfo',
            'iiprop': 'url|mime|extmetadata', 'iiurlwidth': 1000})
        pages = payload.get('query', {}).get('pages', [])
        pages = list(pages.values()) if isinstance(pages, dict) else pages
        for page in pages[:5]:
            infos = page.get('imageinfo') or []
            if not infos:
                continue
            info = infos[0]
            meta = {key: _plain(item.get('value')) for key, item in info.get('extmetadata', {}).items()
                    if isinstance(item, dict)}
            description = meta.get('ImageDescription', '')
            categories = meta.get('Categories', '')
            title = page.get('title', '').replace('_', ' ')
            if not (_contains_name(title, attempt) or _contains_name(description, attempt)
                    or _contains_name(categories, attempt)):
                continue
            # A bitmap alone does not establish a real photograph.
            photo_basis = ' '.join([meta.get('Categories', ''), description, meta.get('ObjectName', '')])
            if (info.get('mime') not in {'image/jpeg', 'image/png'}
                    or not re.search(r'photograph|\bphoto\b|照片|摄影|攝影', photo_basis, re.I)
                    or re.search(r'AI.generated|generative|illustration|painting|drawing|绘画|插画', photo_basis, re.I)):
                continue
            author, license_ = meta.get('Artist'), meta.get('LicenseShortName')
            url, source = info.get('thumburl') or info.get('url'), info.get('descriptionurl')
            if not author or not license_ or not _safe_url(source, {'commons.wikimedia.org'}):
                continue
            if not _safe_url(url, {'upload.wikimedia.org', 'thumb.wikimedia.org'}):
                raise MediaError('image_host', 'Commons returned an unsafe image URL')
            self._image_ready(url)
            return {'url': url, 'source_url': source, 'caption': original_name + '；拍摄时间与季节见出处',
                    'attribution': author + ' · ' + license_, 'license': license_,
                    'match_basis': 'literal_name_and_photograph_metadata', 'status': '未交叉确认'}
        raise MediaError('photo_no_named_match', 'No attributed photograph matched the saved place name')

    def _geocode_candidates(self, name):
        """Exact-name geocoder matches, preferring an attempt that resolves to one.

        Tries the saved name, then its base name (parenthetical qualifier
        stripped). Returns the first attempt's matches when no attempt resolves
        uniquely, so the caller can still report ambiguity.
        """
        attempts = [name]
        base = _base_name(name)
        if base != name:
            attempts.append(base)
        first_matches = first_params = None
        for attempt in attempts:
            params = {'q': attempt, 'format': 'jsonv2', 'limit': 5, 'namedetails': 1}
            results = self.fetch(GEOCODER, params)
            if not isinstance(results, list):
                raise MediaError('geocode_response', 'Expected a geocoder result list')
            matches = []
            for item in results[:5]:
                aliases = [item.get('name', '')] + list((item.get('namedetails') or {}).values())
                if any(_normal(str(alias)) == _normal(attempt) for alias in aliases):
                    matches.append(item)
            if len(matches) == 1:
                return matches, params
            if matches and first_matches is None:
                first_matches, first_params = matches, params
        if first_matches:
            return first_matches, first_params
        raise MediaError('geocode_no_exact_match', 'Expected exactly one exact-name geocoder result')

    def _geocode(self, name):
        # Strict: more than one exact-name match is ambiguous here. Collapsing
        # same-place duplicates happens only in the anchor pass, where the
        # day's located stops prove which city the name belongs to — collapsing
        # in this single-stop context once plotted a Ningbo stop on a Hangzhou
        # day, because the two Ningbo entries happen to sit close together.
        matches, params = self._geocode_candidates(name)
        if len(matches) != 1:
            raise MediaError('geocode_ambiguous' if len(matches) > 1 else 'geocode_no_exact_match',
                             'Expected exactly one exact-name geocoder result')
        item = matches[0]
        lat, lon = _point(item['lat'], item['lon'])
        return {'name': name, 'lat': lat, 'lon': lon,
                'source_url': GEOCODER + '?' + urlencode(params), 'status': '未交叉确认'}

    def _map(self, stops, unlocated=None):
        if not stops:
            raise MediaError('map_no_stops', 'Day has no named stops')
        width, height = 640, 320
        for zoom in range(15, -1, -1):
            points = [_project(stop['lat'], stop['lon'], zoom) for stop in stops]
            xs, ys = zip(*points)
            if max(xs) - min(xs) <= width - 100 and max(ys) - min(ys) <= height - 90:
                break
        left, top = (min(xs) + max(xs) - width) / 2, (min(ys) + max(ys) - height) / 2
        tiles = []
        for x in range(math.floor(left / 256), math.floor((left + width) / 256) + 1):
            for y in range(math.floor(top / 256), math.floor((top + height) / 256) + 1):
                if not 0 <= y < 2 ** zoom:
                    continue
                url = f'https://tile.openstreetmap.org/{zoom}/{x % 2 ** zoom}/{y}.png'
                # Embed the tile bytes: the PDF renderer must not re-fetch
                # twenty tiles over a flaky proxy at print time — one stalled
                # tile used to refuse the whole PDF.
                mime, body = self._image_ready(url)
                data_uri = 'data:' + mime + ';base64,' + b64encode(body).decode('ascii')
                tiles.append(f'<img src="{data_uri}" alt="OSM 底图" style="position:absolute;'
                             f'left:{x * 256 - left:.1f}px;top:{y * 256 - top:.1f}px;'
                             'width:256px;height:256px;max-width:none">')
        markers = []
        for index, (stop, (x, y)) in enumerate(zip(stops, points), 1):
            x, y = x - left, y - top
            markers.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="10" fill="#194e4b"/>'
                           f'<text x="{x:.1f}" y="{y + 4:.1f}" text-anchor="middle" '
                           f'fill="white" font-size="11">{index}</text>')
        labels = ' · '.join(f'{i}. {html.escape(stop["name"])}' for i, stop in enumerate(stops, 1))
        if unlocated:
            labels += '；未定位：' + '、'.join(html.escape(name) for name in unlocated)
        # Markers show locations only; no fabricated road geometry or travel durations.
        return ('<figure class="handbook-map" data-handbook-map="osm" style="break-inside:avoid">'
                '<div style="overflow:auto;max-width:100%"><div style="position:relative;'
                'overflow:hidden;width:640px;height:320px">' + ''.join(tiles)
                + '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 320" '
                'style="position:absolute;inset:0;width:640px;height:320px" role="img" '
                'aria-label="当日停靠点位置">' + ''.join(markers) + '</svg></div></div>'
                '<figcaption>' + labels + '；编号为行程顺序，仅标位置。'
                '<a href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors</a>'
                '；位置为地理编码单源，未交叉确认。</figcaption></figure>')

    def enrich(self, detail):
        output, records, failures = deepcopy(detail), [], []
        photos, coordinates = {}, {}
        for day_index, day in enumerate(output.get('days', [])):
            path = f'$.days[{day_index}]'
            day.pop('_presentation_map_html', None)
            for spot_index, spot in enumerate(day.get('attractions', [])):
                field = f'{path}.attractions[{spot_index}].images'
                try:
                    name = _name(spot.get('name'))
                    if not name:
                        raise MediaError('media_place_name', 'A saved place name is required')
                    existing = [item for item in spot.get('images', []) if isinstance(item, dict)
                                and item.get('attribution') and _safe_url(item.get('source_url'), {'commons.wikimedia.org'})
                                and _safe_url(item.get('url'), {'upload.wikimedia.org', 'thumb.wikimedia.org'})]
                    if existing:
                        image = existing[0]
                        self._image_ready(image['url'])
                    else:
                        if name not in photos:
                            photos[name] = self._photo(name)
                        image = deepcopy(photos[name])
                    spot['images'] = [image]
                    records.append({'kind': 'photo', 'field_path': field, 'name': name, **image})
                except Exception as error:
                    spot['images'] = []
                    failures.append(_failure(field, 'photo', error))
        # Map stage, three passes: (1) resolve every unambiguous stop, one day
        # at a time; (2) disambiguate the rest against the whole trip's located
        # stops, not one day's — a day whose every stop is ambiguous has no
        # anchor of its own; (3) build each day's map from its resolved stops.
        day_maps = []
        for day_index, day in enumerate(output.get('days', [])):
            path = f'$.days[{day_index}]'
            state = {'day': day, 'path': path, 'mapped': [], 'unlocated': [], 'pending': []}
            day_maps.append(state)
            try:
                stops = day.get('stops') or day.get('attractions', [])
                for stop_index, stop in enumerate(stops):
                    name = _name(stop.get('name'))
                    if not name:
                        state['unlocated'].append(str(stop.get('name') or ''))
                        continue
                    try:
                        if name not in coordinates:
                            coordinates[name] = self._geocode(name)
                        state['mapped'].append((stop_index, coordinates[name]))
                    except MediaError as error:
                        if error.rule == 'geocode_ambiguous':
                            state['pending'].append((stop_index, name))
                        else:
                            state['unlocated'].append(name)
                            failures.append(_failure(f'{path}.stops[{stop_index}]', 'map', error))
                    except Exception as error:
                        state['unlocated'].append(name)
                        failures.append(_failure(f'{path}.stops[{stop_index}]', 'map', error))
            except Exception as error:
                failures.append(_failure(path + '._presentation_map_html', 'map', error))
        anchors = [point for state in day_maps for _, point in state['mapped']]
        if anchors:
            # An ambiguous stop resolves only when one of its candidates sits
            # near the trip's located stops (same city area, 50 km). Same-name
            # places in other cities stay unlocated — a wrong-city marker is
            # worse than no marker at all.
            anchor_lat = sorted(p['lat'] for p in anchors)[len(anchors) // 2]
            anchor_lon = sorted(p['lon'] for p in anchors)[len(anchors) // 2]
            for state in day_maps:
                for stop_index, name in state['pending']:
                    try:
                        matches, params = self._geocode_candidates(name)
                        near = [m for m in matches
                                if _haversine_km(anchor_lat, anchor_lon,
                                                 float(m['lat']), float(m['lon'])) <= 50]
                        if len(near) > 1:
                            first = near[0]
                            if all(_haversine_km(float(first['lat']), float(first['lon']),
                                                 float(m['lat']), float(m['lon'])) <= 0.5
                                   for m in near[1:]):
                                near = near[:1]
                        if len(near) != 1:
                            raise MediaError('geocode_ambiguous',
                                             'Expected exactly one exact-name geocoder result')
                        lat, lon = _point(near[0]['lat'], near[0]['lon'])
                        coordinates[name] = {'name': name, 'lat': lat, 'lon': lon,
                                           'source_url': GEOCODER + '?' + urlencode(params),
                                           'status': '未交叉确认'}
                        state['mapped'].append((stop_index, coordinates[name]))
                    except Exception as error:
                        state['unlocated'].append(name)
                        failures.append(_failure(f"{state['path']}.stops[{stop_index}]", 'map', error))
        else:
            for state in day_maps:
                for stop_index, name in state['pending']:
                    state['unlocated'].append(name)
                    failures.append(_failure(f"{state['path']}.stops[{stop_index}]", 'map',
                        MediaError('geocode_ambiguous', 'Expected exactly one exact-name geocoder result')))
        for state in day_maps:
            day, path = state['day'], state['path']
            state['mapped'].sort(key=lambda item: item[0])
            points = [point for _, point in state['mapped']]
            try:
                if not points:
                    failures.append(_failure(path + '._presentation_map_html', 'map',
                        MediaError('map_no_located_stops', 'No stop on this day could be geocoded')))
                else:
                    day['_presentation_map_html'] = self._map(points, state['unlocated'])
                    records.append({'kind': 'map', 'field_path': path + '._presentation_map_html',
                                    'stops': deepcopy(points), 'unlocated': list(state['unlocated']),
                                    'source_url': 'https://www.openstreetmap.org/copyright',
                                    'attribution': '© OpenStreetMap contributors', 'status': '未交叉确认'})
            except Exception as error:
                failures.append(_failure(path + '._presentation_map_html', 'map', error))
        for record in records:
            record['checked_at'] = datetime.now(timezone.utc).isoformat()
        return output, records, failures


def enrich(detail):
    return RuntimeMediaProvider().enrich(detail)
