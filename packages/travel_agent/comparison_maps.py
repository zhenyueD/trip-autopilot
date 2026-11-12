"""Bounded, on-demand Google Maps reference; Google content stays in memory.

Only reserve/record callbacks may persist safe request metadata. Callers must not
serialize the returned UI result into cards, model prompts, logs, or screenshots.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .execution_budget import ExecutionBudget, run_with_deadline
from .writer_transport_http import _open_response

PLACES_URL = 'https://places.googleapis.com/v1/places:searchText'
ROUTES_URL = 'https://routes.googleapis.com/directions/v2:computeRoutes'
MAX_RESPONSE_BYTES = 64 * 1024
_ALLOWED_REASONS = frozenset({
    'API_KEY_INVALID', 'API_KEY_EXPIRED', 'API_KEY_SERVICE_BLOCKED',
    'API_KEY_HTTP_REFERRER_BLOCKED', 'API_KEY_IP_ADDRESS_BLOCKED',
    'SERVICE_DISABLED', 'BILLING_DISABLED', 'CONSUMER_INVALID',
    'RATE_LIMIT_EXCEEDED', 'ACCESS_TOKEN_SCOPE_INSUFFICIENT',
})
_ALLOWED_STATUSES = frozenset({
    'PERMISSION_DENIED', 'UNAUTHENTICATED', 'RESOURCE_EXHAUSTED',
    'INVALID_ARGUMENT', 'NOT_FOUND', 'UNAVAILABLE', 'INTERNAL',
    'DEADLINE_EXCEEDED', 'FAILED_PRECONDITION', 'UNKNOWN',
})
_SAFE_FIELDS = ('attempt_id', 'operation', 'elapsed_ms', 'http_status',
                'delivery_state', 'error_code', 'provider_request_id',
                'body_sha256', 'place_id', 'error_reason')


@dataclass(frozen=True)
class ConfirmedOrigin:
    name: str
    address: str
    source_url: str
    confirmed: bool = False


@dataclass(frozen=True)
class ConfirmedDestination:
    name: str
    address: str
    source_url: str
    confirmed: bool = False


def server_key_from_environment(environ=None):
    """Only explicit Maps server variables; never borrow a Gemini/browser key."""
    environ = os.environ if environ is None else environ
    for name in ('GOOGLE_MAPS_SERVER_KEY', 'GOOGLE_MAPS_API_KEY'):
        key = environ.get(name)
        if isinstance(key, str) and key.strip():
            return key.strip()
    return None


def _safe_id(value, secret=None):
    if (isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,256}', value)
            and '://' not in value and not (secret and secret in value)):
        return value
    return None


class _Trace:
    """Small implementation of the existing observed HTTP transport contract."""
    def __init__(self, timeout, secret):
        self.started = time.monotonic()
        self.deadline = self.started + timeout
        self.secret = secret
        self.socket = None
        self.delivery_state = 'not_sent'
        self.provider_request_id = None
        self.cancelled = False

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if self.cancelled or remaining <= 0:
            raise TimeoutError('maps_deadline')
        return remaining

    def begin(self, _phase):
        return time.monotonic()

    def complete(self, _phase, _started, _metric=None):
        return None

    def timeout_error(self, _phase=None):
        return TimeoutError('maps_deadline')

    def track_socket(self, value):
        self.socket = value
        if self.cancelled:
            self.cancel()

    def cancel(self):
        self.cancelled = True
        if self.socket is not None:
            try:
                self.socket.close()
            except Exception:
                pass

    def request_send_started(self):
        self.delivery_state = 'sent_unacknowledged'

    def request_complete(self):
        self.delivery_state = 'sent_unacknowledged'

    def headers_received(self, headers):
        self.delivery_state = 'acknowledged'
        for name in ('x-request-id', 'X-Request-Id', 'x-goog-request-id', 'x-cloud-trace-context'):
            value = _safe_id(headers.get(name), self.secret)
            if value:
                self.provider_request_id = value
                break


def _base_result(origin, destination_name=None):
    return {'status': 'unchecked', 'is_live': False, 'mode': 'DRIVE',
            'origin': origin.name if isinstance(origin, ConfirmedOrigin) else None,
            'destination': destination_name, 'scope': 'confirmed_endpoints',
            'distance_meters': None, 'duration_seconds': None,
            'checked_at': None, 'expires_at': None, 'attribution': 'Google Maps',
            'note': '仅以上起终点间的驾车路线参考；非实时路况，也不是公共接驳时刻。'}


def _confirmed_endpoint(endpoint):
    if (not isinstance(endpoint, (ConfirmedOrigin, ConfirmedDestination))
            or endpoint.confirmed is not True or not isinstance(endpoint.name, str)
            or not 1 <= len(endpoint.name.strip()) <= 200
            or not isinstance(endpoint.address, str)
            or not isinstance(endpoint.source_url, str)
            or not 5 <= len(endpoint.address.strip()) <= 300):
        return False
    try:
        source = urllib.parse.urlsplit(endpoint.source_url)
    except (TypeError, ValueError):
        return False
    return (source.scheme == 'https' and bool(source.hostname)
            and not source.username and not source.password)


def _preflight(api_key, origin, timeout, ttl_seconds):
    if (not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 512
            or any(char.isspace() or not char.isascii() for char in api_key)):
        return 'maps_key_unavailable'
    if not isinstance(origin, ConfirmedOrigin) or not _confirmed_endpoint(origin):
        return 'origin_unconfirmed'
    for value, upper in ((timeout, 60), (ttl_seconds, 300)):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= upper:
            return 'invalid_limits'
    return None


def _http_reason(data, secret):
    error = data.get('error') if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return None, None
    status = error.get('status')
    status = status if isinstance(status, str) and status in _ALLOWED_STATUSES else None
    details = error.get('details', [])
    details = details if isinstance(details, list) else []
    reason = next((item['reason'] for item in details if isinstance(item, dict)
                   and isinstance(item.get('reason'), str)
                   and item['reason'] in _ALLOWED_REASONS), None)
    # Only stable enums are returned; provider prose can echo keys or user data.
    return status, reason


def _read_body(response, trace):
    parts, size = [], 0
    reader = getattr(response, 'read1', response.read)
    while True:
        remaining = trace.remaining()
        if trace.socket is not None:
            trace.socket.settimeout(remaining)
        chunk = reader(min(8192, MAX_RESPONSE_BYTES + 1 - size))
        if not chunk:
            return b''.join(parts)
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ValueError('response_too_large')
        parts.append(chunk)


def _candidate(data, expected_name, secret=None):
    places = data.get('places')
    if not isinstance(places, list) or len(places) != 1 or data.get('nextPageToken'):
        return None
    place = places[0]
    if not isinstance(place, dict) or not isinstance(place.get('displayName'), dict):
        return None
    name = place['displayName'].get('text')
    if not isinstance(name, str) or name.strip().casefold() != expected_name.strip().casefold():
        return None
    return _safe_id(place.get('id'), secret)


def _record_result(result, metadata, trace, record):
    metadata.update(elapsed_ms=max(0, round((time.monotonic() - trace.started) * 1000)),
                    delivery_state=trace.delivery_state,
                    provider_request_id=trace.provider_request_id,
                    error_code=result.get('error_code'),
                    error_reason=result.get('error_reason'))
    safe = {key: metadata.get(key) for key in _SAFE_FIELDS}
    if record is not None:
        try:
            record(safe)
        except Exception:
            return {'status': 'preflight_error', 'error_code': 'ledger_write_failed',
                    'http_status': metadata.get('http_status'),
                    'delivery_state': trace.delivery_state}
    return {**result, 'http_status': metadata.get('http_status'),
            'delivery_state': trace.delivery_state}


def _response_result(response, trace, metadata, secret, expected_name):
    status = getattr(response, 'status', None) or getattr(response, 'code', None)
    metadata['http_status'] = status
    trace.headers_received(response.headers)
    try:
        body = _read_body(response, trace)
    except ValueError:
        return {'status': 'response_read_error', 'error_code': 'response_too_large'}
    except Exception:
        return {'status': 'response_read_error', 'error_code': 'response_read_failed'}
    metadata['body_sha256'] = hashlib.sha256(body).hexdigest()
    if status is not None and 300 <= status < 400:
        return {'status': 'http_error', 'error_code': 'redirect_rejected'}
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        return {'status': 'http_error' if status != 200 else 'response_read_error',
                'error_code': 'http_error' if status != 200 else 'invalid_json'}
    if status != 200:
        code, reason = _http_reason(data, secret)
        return {'status': 'http_error', 'error_code': code or 'http_error',
                'error_reason': reason}
    if not isinstance(data, dict):
        return {'status': 'response_read_error', 'error_code': 'invalid_schema'}
    if metadata['operation'] == 'maps_places':
        metadata['place_id'] = _candidate(data, expected_name, secret)
    return {'status': 'ok', 'data': data}


def _exchange(request, trace, metadata, api_key, expected_name):
    response = None
    try:
        trace.remaining()
        try:
            response = _open_response(request, trace)
        except urllib.error.HTTPError as error:
            response = error
        return _response_result(response, trace, metadata, api_key, expected_name)
    except Exception:
        return {'status': 'transport_error', 'error_code': 'transport_failed'}
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass  # Cleanup cannot replace the known HTTP / delivery result.


def _fetch(operation, url, payload, field_mask, *, api_key, reserve, record,
           timeout, place_id=None, expected_name=''):
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method='POST',
                                    headers={'Content-Type': 'application/json',
                                             'X-Goog-Api-Key': api_key,
                                             'X-Goog-FieldMask': field_mask})
    try:
        attempt_id = reserve(operation)
        if not _safe_id(attempt_id, api_key):
            raise ValueError('invalid reservation')
    except Exception:
        return {'status': 'preflight_error', 'error_code': 'budget_exhausted',
                'http_status': None, 'delivery_state': 'not_sent'}
    trace = _Trace(timeout, api_key)
    metadata = {'attempt_id': attempt_id, 'operation': operation, 'place_id': place_id}
    # Reuse the uncharged deadline fence; durable reserve above owns billing.
    fence = ExecutionBudget(seconds=timeout, max_calls=0, label='maps-request')
    bounded = run_with_deadline(
        lambda: _exchange(request, trace, metadata, api_key, expected_name), fence)
    if bounded.status == 'ok':
        result = bounded.value
    else:
        trace.cancel()
        phase = 'response_read_error' if metadata.get('http_status') else 'transport_error'
        result = {'status': phase, 'error_code': 'deadline_exceeded'}
    return _record_result(result, metadata, trace, record)


def _candidates(data, secret):
    places = data.get('places', [])
    if not isinstance(places, list):
        return []
    candidates = []
    for place in places[:2]:
        if not isinstance(place, dict):
            continue
        name = place.get('displayName', {})
        raw = {'place_id': _safe_id(place.get('id'), secret),
               'name': name.get('text') if isinstance(name, dict) else None,
               'address': place.get('formattedAddress')}
        candidates.append({key: value.replace(secret, '[redacted]')
                           if isinstance(value, str) else None for key, value in raw.items()})
    return candidates


def _metrics(data):
    routes = data.get('routes')
    if not isinstance(routes, list) or len(routes) != 1 or not isinstance(routes[0], dict):
        return None
    distance, duration = routes[0].get('distanceMeters'), routes[0].get('duration')
    if type(distance) is not int or distance < 0 or not isinstance(duration, str):
        return None
    if not re.fullmatch(r'\d+(?:\.\d{1,9})?s', duration):
        return None
    seconds = float(duration[:-1])
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return {'distance_meters': distance, 'duration_seconds': seconds}


def _timestamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace('+00:00', 'Z')


def _resolve_destination(name, country, region_code, max_requests, shared):
    query = {'textQuery': f'{name}, {country}', 'pageSize': 2, 'languageCode': 'en'}
    if region_code:
        query['regionCode'] = region_code
    places = _fetch('maps_places', PLACES_URL,
                    query, 'places.id,places.displayName,places.formattedAddress,'
                    'places.attributions,nextPageToken', expected_name=name, **shared)
    if places['status'] != 'ok':
        return None, places
    entries = places['data'].get('places', [])
    if isinstance(entries, list) and any(isinstance(item, dict) and item.get('attributions')
                                         for item in entries):
        return None, {'status': 'awaiting_confirmation',
                      'error_code': 'additional_attribution_required'}
    place_id = _candidate(places['data'], name, shared['api_key'])
    if place_id is None:
        return None, {'status': 'awaiting_confirmation', 'error_code': 'destination_unconfirmed',
                      'candidates': _candidates(places['data'], shared['api_key'])}
    if max_requests == 1:
        return None, {'status': 'destination_resolved', 'error_code': 'routes_not_requested',
                      'place_id': place_id}
    return {'placeId': place_id}, None


def _route_result(result, origin, waypoint, ttl_seconds, shared):
    routes = _fetch('maps_routes', ROUTES_URL,
                   {'origin': {'address': origin.address}, 'destination': waypoint,
                    'travelMode': 'DRIVE', 'routingPreference': 'TRAFFIC_UNAWARE',
                    'computeAlternativeRoutes': False, 'units': 'METRIC'},
                   'routes.distanceMeters,routes.duration', place_id=waypoint.get('placeId'), **shared)
    if routes['status'] != 'ok':
        return {**result, **routes}
    metrics = _metrics(routes['data'])
    if metrics is None:
        return {**result, 'status': 'response_read_error', 'error_code': 'route_unavailable',
                'http_status': routes['http_status'], 'delivery_state': routes['delivery_state']}
    now = time.time()
    return {**result, **metrics, 'status': 'ready', 'place_id': waypoint.get('placeId'),
            'checked_at': _timestamp(now), 'expires_at': _timestamp(now + ttl_seconds),
            'http_status': routes['http_status'], 'delivery_state': routes['delivery_state']}


def lookup_public_route(*, api_key, origin, reserve, destination_name=None, country=None,
                        region_code=None, destination=None, record=None, timeout=20.0,
                        ttl_seconds=300, max_requests=2):
    """One route for confirmed addresses; otherwise resolve one Places query.

    Missing destination confirmation may use two requests. A one-request cap
    resolves only the destination unless both addresses are already confirmed.
    """
    name = destination.name if isinstance(destination, ConfirmedDestination) else destination_name
    result = _base_result(origin, name)
    issue = _preflight(api_key, origin, timeout, ttl_seconds)
    if type(max_requests) is not int or max_requests not in (1, 2):
        issue = issue or 'invalid_limits'
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
        issue = issue or 'destination_unconfirmed'
    confirmed = isinstance(destination, ConfirmedDestination) and _confirmed_endpoint(destination)
    if destination is not None and not confirmed:
        issue = issue or 'destination_unconfirmed'
    if not confirmed and (not isinstance(country, str) or not 1 <= len(country.strip()) <= 100):
        issue = issue or 'country_required'
    if region_code is not None and (not isinstance(region_code, str)
                                    or not re.fullmatch(r'[A-Z]{2}', region_code)):
        issue = issue or 'invalid_region'
    if issue:
        return {**result, 'status': 'preflight_error', 'error_code': issue,
                'delivery_state': 'not_sent', 'http_status': None}
    shared = dict(api_key=api_key, reserve=reserve, record=record, timeout=timeout)
    if confirmed:
        waypoint = {'address': destination.address}
    else:
        waypoint, resolved = _resolve_destination(name, country, region_code, max_requests, shared)
        if resolved is not None:
            now = time.time()
            return {**result, **resolved, 'checked_at': _timestamp(now),
                    'expires_at': _timestamp(now + ttl_seconds)}
    return _route_result(result, origin, waypoint, ttl_seconds, shared)


def expire_route(route, now=None):
    """Clear transient provider content when the in-memory display window ends."""
    if not isinstance(route, dict):
        return _base_result(None)
    expiry = route.get('expires_at')
    if not expiry:
        return dict(route)
    try:
        expires = datetime.fromisoformat(expiry.replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError, OverflowError):
        expires = 0
    if (time.time() if now is None else now) < expires:
        return dict(route)
    cleared = {key: value for key, value in route.items() if key not in
               ('candidates', 'distance_meters', 'duration_seconds', 'checked_at')}
    return {**cleared, 'status': 'expired', 'distance_meters': None,
            'duration_seconds': None, 'checked_at': None}
