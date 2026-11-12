"""One credential-free, read-only SevenRooms availability channel.

This pilot has one server-owned venue association. Public opening text, search
snippets, reservation requests and holds never become available inventory.
Transport injection is for offline tests/replay and is labeled as such. SHA-256
receipts provide content binding, not a cryptographic attestation of the vendor.
No booking/hold endpoint, cookies, credentials, redirects or retry are supported.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import http.client
import json
import queue
import re
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

from .material_reader import _ReadDeadline, _connect_pinned, _read_body


@dataclass(frozen=True)
class RestaurantBranch:
    branch_id: str
    name: str
    address: str
    timezone: str
    official_url: str
    booking_url: str
    venue_id: str


BRANCH = RestaurantBranch(
    'bastadubai', 'BASTA! — The St. Regis Downtown Dubai',
    'Marasi Drive, Business Bay, Dubai, United Arab Emirates', 'Asia/Dubai',
    'https://www.basta-dubai.com/', 'https://www.sevenrooms.com/reservations/bastadubai',
    'ahNzfnNldmVucm9vbXMtc2VjdXJlchwLEg9uaWdodGxvb3BfVmVudWUYgIDsx-_T0AsM')
AVAILABILITY_URL = 'https://www.sevenrooms.com/api-yoa/availability/widget/range'
MAX_RESPONSE_BYTES = 262144
VALIDITY_SECONDS = 120
TOTAL_TIMEOUT_SECONDS = 30
_WORKERS = threading.BoundedSemaphore(4)
_PUBLIC_FAILURE_REASONS = frozenset({
    'unsupported_branch_or_timezone', 'invalid_party_size', 'invalid_date', 'invalid_time',
    'past_request', 'missing_receipt_identity', 'transport_busy', 'invalid_transport_response',
    'response_url_mismatch_or_redirect', 'http_unavailable', 'empty_or_oversized_response',
    'unexpected_content_type', 'expired_or_future_response', 'official_booking_association_missing',
    'venue_identity_missing', 'venue_identity_mismatch', 'invalid_slot_state',
    'slot_date_or_time_mismatch', 'slot_timezone_or_branch_mismatch', 'provider_unavailable',
    'availability_date_missing', 'invalid_shift', 'missing_shift_state', 'truncated_response',
    'unsupported_encoding'})


@dataclass(frozen=True)
class PublicResponse:
    requested_url: str
    final_url: str
    http_status: int
    headers: dict
    body: bytes
    observed_at: datetime


def _iso_clock(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('aware_timestamp_required')
    return value.astimezone(timezone.utc)


def _conditions(branch_id, local_date, local_time, timezone_name, party_size, now):
    if branch_id != BRANCH.branch_id or timezone_name != BRANCH.timezone:
        raise ValueError('unsupported_branch_or_timezone')
    if type(party_size) is not int or not 1 <= party_size <= 20:
        raise ValueError('invalid_party_size')
    if not isinstance(local_date, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', local_date):
        raise ValueError('invalid_date')
    if not isinstance(local_time, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', local_time):
        raise ValueError('invalid_time')
    requested = datetime.fromisoformat(local_date + 'T' + local_time).replace(tzinfo=ZoneInfo(timezone_name))
    if requested <= now:
        raise ValueError('past_request')
    return {'branch_id': branch_id, 'date': local_date, 'time': local_time,
            'timezone': timezone_name, 'party_size': party_size}


def _query_parameters(conditions):
    date = datetime.strptime(conditions['date'], '%Y-%m-%d').strftime('%m-%d-%Y')
    return {'venue': BRANCH.branch_id, 'time_slot': conditions['time'],
            'party_size': str(conditions['party_size']), 'halo_size_interval': '16',
            'start_date': date, 'num_days': '1', 'channel': 'SEVENROOMS_WIDGET',
            'selected_lang_code': 'en'}


def _allowed_url(url):
    """Exact host/path/query allowlist; callers cannot grant arbitrary origins."""
    if url in (BRANCH.official_url, BRANCH.booking_url):
        return urlsplit(url)
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.netloc != 'www.sevenrooms.com'
            or parsed.path != '/api-yoa/availability/widget/range' or parsed.fragment):
        raise ValueError('unsupported_public_endpoint')
    params = parse_qs(parsed.query, strict_parsing=True)
    expected = {'venue', 'time_slot', 'party_size', 'halo_size_interval', 'start_date',
                'num_days', 'channel', 'selected_lang_code'}
    if set(params) != expected or any(len(values) != 1 for values in params.values()):
        raise ValueError('unsupported_query_parameters')
    p = {key: values[0] for key, values in params.items()}
    date = datetime.strptime(p['start_date'], '%m-%d-%Y').strftime('%Y-%m-%d')
    validated = _conditions(p['venue'], date, p['time_slot'], BRANCH.timezone,
                            int(p['party_size']), datetime.min.replace(tzinfo=timezone.utc))
    if p != _query_parameters(validated):
        raise ValueError('unsupported_query_parameters')
    return parsed


def default_transport(url, *, deadline):
    """Exactly one bounded GET using the existing DNS-pinned TLS connector.

    Root gateway may wrap this callable to debit each of the three actual GETs.
    Redirect responses are returned as failures, never followed invisibly.
    """
    parsed = _allowed_url(url)
    budget = _ReadDeadline()
    budget.end = min(budget.end, deadline)
    connection = None
    try:
        budget.remaining()
        sock = _connect_pinned(parsed, budget)
        connection = http.client.HTTPConnection(parsed.hostname, 443)
        connection.sock = sock
        target = parsed.path + ('?' + parsed.query if parsed.query else '')
        sock.settimeout(budget.remaining())
        connection.request('GET', target, headers={
            'User-Agent': 'TravelAvailabilityPilot/1.0', 'Accept-Encoding': 'identity',
            'Accept': 'application/json,text/html', 'Connection': 'close'})
        response = connection.getresponse()
        headers = {k.lower(): v for k, v in response.getheaders()}
        if headers.get('content-encoding', 'identity').lower() != 'identity':
            raise ValueError('unsupported_encoding')
        body, truncated = _read_body(response, sock, budget)
        if truncated:
            raise ValueError('truncated_response')
        return PublicResponse(url, url, response.status, headers, body, datetime.now(timezone.utc))
    finally:
        if connection is not None:
            connection.close()
        budget.cancel()


def _call_transport(transport, url, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('timeout')
    if not _WORKERS.acquire(blocking=False):
        raise ValueError('transport_busy')
    output = queue.Queue(maxsize=1)

    def worker():
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError('timeout')
            output.put((True, transport(url, deadline=deadline)))
        except Exception as error:
            output.put((False, error))
        finally:
            _WORKERS.release()

    from .phase1_budget_context import context_target
    threading.Thread(target=context_target(worker), name='restaurant-availability', daemon=True).start()
    try:
        success, value = output.get(timeout=max(0, deadline - time.monotonic()))
    except queue.Empty:
        raise TimeoutError('timeout') from None
    if time.monotonic() >= deadline:
        raise TimeoutError('timeout')
    if not success:
        raise value
    return value


def _check_response(response, url, now):
    if not isinstance(response, PublicResponse):
        raise ValueError('invalid_transport_response')
    if response.requested_url != url or response.final_url != url:
        raise ValueError('response_url_mismatch_or_redirect')
    if response.http_status != 200:
        raise ValueError('http_unavailable')
    if not isinstance(response.body, bytes) or not 0 < len(response.body) <= MAX_RESPONSE_BYTES:
        raise ValueError('empty_or_oversized_response')
    expected = 'application/json' if url.startswith(AVAILABILITY_URL + '?') else 'text/html'
    if response.headers.get('content-type', '').split(';')[0].strip().lower() != expected:
        raise ValueError('unexpected_content_type')
    observed = _iso_clock(response.observed_at)
    if observed > now + timedelta(seconds=5) or now >= observed + timedelta(seconds=VALIDITY_SECONDS):
        raise ValueError('expired_or_future_response')


class _BookingLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.matched = False

    def handle_starttag(self, tag, attrs):
        if tag != 'a':
            return
        target = dict(attrs).get('href', '')
        parsed, expected = urlsplit(target), urlsplit(BRANCH.booking_url)
        if (parsed.scheme, parsed.netloc, parsed.path) == (expected.scheme, expected.netloc, expected.path):
            self.matched = True


def _check_official(body):
    parser = _BookingLinks()
    parser.feed(body.decode('utf-8'))
    if not parser.matched:
        raise ValueError('official_booking_association_missing')


def _check_identity(body):
    match = re.search(r'venueInfo:\s*JSON.parse\(("(?:[^"\\]|\\.)*")\)', body.decode('utf-8'))
    if not match:
        raise ValueError('venue_identity_missing')
    identity = json.loads(json.loads(match[1]))
    if not isinstance(identity, dict) or any(identity.get(key) != value for key, value in {
            'url_key': BRANCH.branch_id, 'venue_id': BRANCH.venue_id, 'timezone': BRANCH.timezone}.items()):
        raise ValueError('venue_identity_mismatch')


def _normalize_slot(raw, conditions):
    if not isinstance(raw, dict):
        raise ValueError('invalid_slot')
    if raw.get('type') == 'request':
        return None
    if raw.get('type') != 'book' or type(raw.get('is_held')) is not bool:
        raise ValueError('invalid_slot_state')
    if raw['is_held']:
        return None
    local = datetime.strptime(raw['time_iso'], '%Y-%m-%d %H:%M:%S')
    if (local.date().isoformat() != conditions['date'] or local.second != 0
            or raw.get('time') != local.strftime('%H:%M')
            or raw.get('real_datetime_of_slot') != raw['time_iso']):
        raise ValueError('slot_date_or_time_mismatch')
    zoned = local.replace(tzinfo=ZoneInfo(conditions['timezone']))
    utc = datetime.strptime(raw['utc_datetime'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    if utc != zoned or not str(raw.get('access_persistent_id', '')).startswith(BRANCH.venue_id + '-'):
        raise ValueError('slot_timezone_or_branch_mismatch')
    return {'conditions': {**conditions, 'time': local.strftime('%H:%M')},
            'local_datetime': zoned.isoformat(), 'utc_datetime': utc.isoformat(),
            'description': raw.get('public_time_slot_description'),
            'duration_minutes': raw.get('duration'),
            'requires_credit_card': raw.get('require_credit_card'),
            'source_slot_id': raw['access_persistent_id'], 'reservation_status': 'not_attempted'}


def _slots(body, conditions):
    payload = json.loads(body)
    if not isinstance(payload, dict) or payload.get('status') != 200:
        raise ValueError('provider_unavailable')
    shifts = payload['data']['availability'].get(conditions['date'])
    if not isinstance(shifts, list):
        raise ValueError('availability_date_missing')
    slots = []
    for shift in shifts:
        if not isinstance(shift, dict) or not isinstance(shift.get('times'), list):
            raise ValueError('invalid_shift')
        flags = ('is_closed', 'is_previous_version', 'is_forced_empty_availability')
        if any(type(shift.get(key)) is not bool for key in flags):
            raise ValueError('missing_shift_state')
        if any(shift[key] for key in flags):
            continue
        for raw in shift['times']:
            slot = _normalize_slot(raw, conditions)
            if slot is not None and slot not in slots:
                slots.append(slot)
    return slots


def _source_receipt(response):
    return {'method': 'GET', 'source_ref': response.requested_url,
            'final_url': response.final_url, 'http_status': response.http_status,
            'body_sha256': hashlib.sha256(response.body).hexdigest(),
            'bytes_read': len(response.body), 'observed_at': response.observed_at.isoformat(),
            'published_at': None, 'redirects_followed': 0, 'authentication': 'none'}


def _evidence(slot, response, params, scope_id, tool_call_id, capture_mode, receipts):
    observed = _iso_clock(response.observed_at)
    expiry = min(observed + timedelta(seconds=VALIDITY_SECONDS),
                 datetime.fromisoformat(slot['utc_datetime']))
    content = {'slot': slot, 'availability_status': 'available', 'query_parameters': params,
               'condition_binding': 'slot decoded from response to this range query; not a separate query',
               'provenance_receipts': receipts, 'capture_mode': capture_mode,
               'opening_status': 'unknown', 'reservation_status': 'not_attempted'}
    return {'scope_id': scope_id, 'tool_call_id': tool_call_id, 'entity_id': BRANCH.branch_id,
            'source_ref': response.requested_url, 'source_identity': BRANCH.branch_id,
            'source_family': 'sevenrooms_public_availability', 'raw_status': 'read',
            'request_conditions': dict(slot['conditions']),
            'content': json.dumps(content, ensure_ascii=False, sort_keys=True),
            'observed_at': observed.isoformat(), 'published_at': None,
            'valid_from': observed.isoformat(), 'valid_until': expiry.isoformat(),
            'http_status': response.http_status, 'body_sha256': hashlib.sha256(response.body).hexdigest(),
            'validity_basis': 'local 120-second recheck policy; not a vendor guarantee',
            'untrusted_external_content': True, 'capture_mode': capture_mode}


def _select_result(result, response, conditions, params, scope_id, tool_call_id, checked_at):
    # I/O may cross the requested slot boundary. Apply the clock again after
    # receiving the result, to both the primary slot and every alternative.
    observed = _iso_clock(response.observed_at)
    if observed + timedelta(seconds=VALIDITY_SECONDS) <= checked_at:
        raise ValueError('availability_response_expired')
    slots = [slot for slot in _slots(response.body, conditions)
             if datetime.fromisoformat(slot['utc_datetime']) > max(checked_at, observed)]
    exact = next((slot for slot in slots if slot['conditions']['time'] == conditions['time']), None)
    others = [slot for slot in slots if slot['conditions']['time'] != conditions['time']]
    target = datetime.fromisoformat(conditions['date'] + 'T' + conditions['time'])
    others.sort(key=lambda slot: (slot['conditions']['time'] < conditions['time'],
                                 abs((datetime.fromisoformat(slot['local_datetime']).replace(tzinfo=None) - target).total_seconds())))
    alternatives = []
    for slot in others:
        if not any(x['conditions']['time'] == slot['conditions']['time'] for x in alternatives):
            alternatives.append(slot)
        if len(alternatives) == 2:
            break
    result.update(status='available' if exact else 'not_available',
                  reason='exact_slot_returned' if exact else 'no_exact_online_slot_returned',
                  slot=exact, alternatives=alternatives)
    selected = ([exact] if exact else []) + alternatives
    result['evidence'] = [_evidence(slot, response, params, scope_id, tool_call_id,
                                  result['capture_mode'], result['tool_receipt']['requests'])
                          for slot in selected]


def query_restaurant_availability(*, branch_id, local_date, local_time, timezone_name,
                                  party_size, scope_id, tool_call_id, transport=None,
                                  now=None, deadline=None, evidence_sink=None):
    """Query the fixed public channel; unknown on any failed or expired premise.

    ``deadline`` is the caller's shared monotonic deadline; this query also has
    a 30-second total ceiling. The three individual GETs are exposed via the
    injectable/default transport for upstream budget accounting. ``now`` freezes
    the clock only for explicit historical replay. ``evidence_sink`` receives
    (receipt, raw bytes) before interpretation, including failed HTTP responses.
    """
    capture_mode = 'live_read' if transport is None and now is None else 'injected_transport'
    result = {'status': 'unknown', 'reason': None, 'slot': None, 'alternatives': [],
              'evidence': [], 'opening_status': 'unknown', 'reservation_status': 'not_attempted',
              'capture_mode': capture_mode, 'branch': {'id': BRANCH.branch_id, 'name': BRANCH.name,
              'address': BRANCH.address}, 'tool_receipt': {'network_call_count': 0, 'requests': [],
              'read_only': True, 'reservation_attempted': False}}
    clock = (lambda: _iso_clock(now)) if now is not None else (lambda: datetime.now(timezone.utc))
    end = min(time.monotonic() + TOTAL_TIMEOUT_SECONDS, deadline if deadline is not None else float('inf'))
    transport = transport or default_transport
    try:
        conditions = _conditions(branch_id, local_date, local_time, timezone_name, party_size, clock())
        if not all(isinstance(x, str) and 0 < len(x) <= 200 for x in (scope_id, tool_call_id)):
            raise ValueError('missing_receipt_identity')
        result['request_conditions'] = conditions
        params = _query_parameters(conditions)
        urls = [BRANCH.official_url, BRANCH.booking_url, AVAILABILITY_URL + '?' + urlencode(params)]
        validators = [_check_official, _check_identity, lambda body: None]
        for url, validator in zip(urls, validators):
            if time.monotonic() >= end:
                raise TimeoutError('timeout')
            result['tool_receipt']['network_call_count'] += 1
            response = _call_transport(transport, url, end)
            if isinstance(response, PublicResponse):
                receipt = _source_receipt(response)
                result['tool_receipt']['requests'].append(receipt)
                if evidence_sink is not None:
                    evidence_sink(dict(receipt), response.body)
            _check_response(response, url, clock())
            validator(response.body)
        _select_result(result, response, conditions, params, scope_id, tool_call_id, clock())
        if time.monotonic() >= end:
            raise TimeoutError('timeout')
    except TimeoutError:
        result.update(status='unknown', reason='timeout', slot=None, alternatives=[], evidence=[])
    except Exception as error:
        reason = str(error) if str(error) in _PUBLIC_FAILURE_REASONS else 'query_failed'
        result.update(status='unknown', reason=reason, error_type=type(error).__name__,
                      slot=None, alternatives=[], evidence=[])
    return result
