"""Read-only road-route evidence for a model-owned itinerary.

The public OSRM demo is a bounded MVP dependency, not a production SLA. Its
driving profile is always labelled driving, regardless of itinerary prose.
Docs: https://project-osrm.org/docs/v5.24.0/api/#route-service
Policy: https://github.com/Project-OSRM/osrm-backend/wiki/Demo-server
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from queue import Empty, Queue
import threading
import time
from urllib.request import build_opener, ProxyHandler, Request

from .geography import located
from .supply import _NoRedirect
from .phase1_budget_context import (
    clamp_deadline, context_target, current_budget, physical_attempt, use_budget)


PROVIDER = 'OSRM / OpenStreetMap'
ENDPOINT = 'https://router.project-osrm.org/route/v1/driving/'
MAX_RESPONSE_BYTES = 1_000_000
REQUEST_TIMEOUT_S = 2.5
CACHE_TTL_S = 6 * 60 * 60
CACHE_LIMIT = 128
NOTE = ('按日程站点顺序取得的驾车路网参考；不是步行或公交路线，不含实时路况、停车或进出场馆时间。'
        '地点坐标可能不是入口；不是逐向导航，实际出发前请在地图中复核。数据 © OpenStreetMap contributors。')
REASONS = {
    'missing_coordinates': '有效坐标的站点不足两个，尚未查到路线；未给未知站点补造坐标。',
    'insufficient_stops': '不足两个有地点的站点，无可查询的站间路线。',
    'timeout': '路线查询超过本轮短时限，尚未取得路网结果。',
    'provider_unavailable': '路线服务暂未返回可用路网结果，未补造距离或时间。',
    'invalid_response': '路线服务返回不完整，未采用该结果。',
    'no_route': '路网服务未查到这些站点之间的完整驾车路线。',
}
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_PROVIDER_LOCK = threading.Lock()
_LAST_REQUEST = 0.0


def _source_url(coordinates):
    points = ';'.join(f'{lng:.6f},{lat:.6f}' for lng, lat in coordinates)
    radiuses = ';'.join('1000' for _ in coordinates)
    return (ENDPOINT + points + '?overview=simplified&geometries=geojson&steps=false'
            '&alternatives=false&generate_hints=false&radiuses=' + radiuses)


def fetch_osrm_route(coordinates, *, timeout_s):
    """Fetch one ordered route; no credentials, arbitrary URLs or redirects.

    The caller applies the wall-clock deadline, including DNS and response read.
    One process-wide lock also prevents overlapping public-provider requests.
    """
    global _LAST_REQUEST
    deadline = clamp_deadline(time.monotonic() + timeout_s)
    if not _PROVIDER_LOCK.acquire(timeout=max(0, deadline - time.monotonic())):
        raise TimeoutError('Route provider busy')
    try:
        delay = max(0, 1.0 - (time.monotonic() - _LAST_REQUEST))
        if time.monotonic() + delay >= deadline:
            raise TimeoutError('Route query budget exhausted')
        if delay:
            time.sleep(delay)
        url = _source_url(coordinates)
        request = Request(url, headers={'User-Agent': 'TravelAgentRouteMVP/1.0',
            'Accept': 'application/json', 'Accept-Encoding': 'identity'}, method='GET')
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        _LAST_REQUEST = time.monotonic()
        with physical_attempt('route_http', max(.001, deadline - time.monotonic())) as attempt:
            with opener.open(request, timeout=attempt.timeout) as response:
                if response.geturl() != url or response.headers.get_content_type() != 'application/json':
                    raise ValueError('Unexpected route response')
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError('Route response too large')
                return json.loads(body)
    finally:
        _PROVIDER_LOCK.release()


def _bounded_fetch(fetcher, coordinates, timeout_s):
    # A socket timeout alone does not bound DNS or a slowly streamed body.
    timeout_s = max(0, clamp_deadline(time.monotonic() + timeout_s) - time.monotonic())
    if timeout_s <= 0:
        raise TimeoutError('Route query deadline exceeded')
    output = Queue(maxsize=1)

    def run():
        try:
            output.put((fetcher(coordinates, timeout_s=timeout_s), None))
        except Exception as error:
            output.put((None, error))

    threading.Thread(target=context_target(run), name='journey-route', daemon=True).start()
    try:
        payload, error = output.get(timeout=max(0, timeout_s))
    except Empty:
        raise TimeoutError('Route query deadline exceeded') from None
    if error:
        raise error
    return payload


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _validated_payload(payload, stop_count):
    if not isinstance(payload, dict) or payload.get('code') != 'Ok':
        return None
    routes = payload.get('routes')
    if not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
        return None
    route = routes[0]
    geometry, legs = route.get('geometry'), route.get('legs')
    if not isinstance(geometry, dict) or geometry.get('type') != 'LineString':
        return None
    points = geometry.get('coordinates')
    if not isinstance(points, list) or not 2 <= len(points) <= 20000:
        return None
    if any(not isinstance(p, list) or len(p) != 2 or not located({'lng': p[0], 'lat': p[1]})
           for p in points):
        return None
    if not isinstance(legs, list) or len(legs) != stop_count - 1:
        return None
    if any(not isinstance(leg, dict) or not _number(leg.get('distance'))
           or not _number(leg.get('duration')) for leg in legs):
        return None
    return {'geometry': deepcopy(geometry), 'legs': [
        {'distance_m': leg['distance'], 'duration_s': leg['duration']} for leg in legs],
        'checked_at': datetime.now(timezone.utc).isoformat()}


def _lookup(coordinates, fetcher, timeout_s):
    key = tuple(coordinates)
    if fetcher is None:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached and time.monotonic() - cached[0] < CACHE_TTL_S:
                return deepcopy(cached[1]), True, None
    if timeout_s <= 0:
        return None, False, 'timeout'
    try:
        payload = _bounded_fetch(fetcher or fetch_osrm_route, coordinates,
                                 min(REQUEST_TIMEOUT_S, timeout_s))
    except TimeoutError:
        return None, False, 'timeout'
    except Exception:
        return None, False, 'provider_unavailable'
    result = _validated_payload(payload, len(coordinates))
    if not result:
        reason = ('no_route' if isinstance(payload, dict) and payload.get('code') in
                  {'NoRoute', 'NoSegment'} else 'invalid_response')
        return None, False, reason
    if fetcher is None:
        with _CACHE_LOCK:
            if key not in _CACHE and len(_CACHE) >= CACHE_LIMIT:
                _CACHE.pop(next(iter(_CACHE)))
            _CACHE[key] = (time.monotonic(), deepcopy(result))
    return result, False, None


def _day_route(day, catalog, fetcher, deadline):
    # Standalone transfer/personal text has no destination of its own. Other
    # unlocated activities remain explicit gaps in a partial known-anchor route.
    stops = [item for item in day.get('items', []) if item.get('supply_id') or
             item.get('activity_kind') not in {'transfer', 'personal'}]
    missing = [item['id'] for item in stops if not located(catalog.get(item.get('supply_id'), {}))]
    stops = [item for item in stops if item['id'] not in missing]
    legs = [{'from_item_id': a['id'], 'to_item_id': b['id'], 'distance_m': None,
             'duration_s': None, 'status': 'unavailable', 'source_url': None}
            for a, b in zip(stops, stops[1:])]
    result = {'day': day['day'], 'status': 'unavailable', 'provider': PROVIDER,
              'mode': 'driving', 'geometry': None, 'legs': legs, 'source_url': None,
              'checked_at': None, 'missing_item_ids': missing, 'cache_hit': False}
    reason = ('missing_coordinates' if missing else 'insufficient_stops') if len(stops) < 2 else None
    if reason:
        return {**result, 'reason': reason, 'note': REASONS[reason] + NOTE}
    coordinates = [(catalog[item['supply_id']]['lng'], catalog[item['supply_id']]['lat'])
                   for item in stops]
    evidence, cache_hit, reason = _lookup(coordinates, fetcher, deadline - time.monotonic())
    source_url = _source_url(coordinates)
    if not evidence:
        return {**result, 'reason': reason, 'source_url': source_url, 'note': REASONS[reason] + NOTE}
    available_legs = [{**leg, **metrics, 'status': 'available', 'source_url': source_url}
                      for leg, metrics in zip(legs, evidence['legs'])]
    note = ('仅已定位站点之间的驾车路网参考，不含未定位安排；不是完整全天路线。' if missing else '') + NOTE
    return {**result, 'status': 'partial' if missing else 'available', 'geometry': evidence['geometry'],
            'legs': available_legs, 'source_url': source_url, 'checked_at': evidence['checked_at'],
            'cache_hit': cache_hit, 'note': note}


def enrich_routes(plan, supplies, *, fetcher=None, timeout_s=8.0):
    """Return route evidence per day without changing plan, supplies or trip order.

    ``fetcher(coordinates, *, timeout_s)`` may be injected for offline tests and
    must return OSRM route JSON. Coordinates are ordered ``(longitude, latitude)``.
    Only default-provider successes are cached. Every day gets a result, including
    explicit unavailable entries when the shared trip deadline has been spent.
    """
    catalog = {s['id']: s for s in supplies}
    deadline = clamp_deadline(time.monotonic() + max(0.0, min(float(timeout_s), 30.0)))
    results = []
    budget = current_budget()
    for day in plan.get('days', []):
        scope = budget
        if budget is not None:
            scope = budget._root.child('L1', f'day-{day["day"]}').child('L2', 'route')
        with use_budget(scope):
            results.append(_day_route(day, catalog, fetcher, deadline))
    return results
