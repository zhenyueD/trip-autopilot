"""No-key Jina transport for public originals; never a source or identity oracle.

The final URL must equal the requested URL: Reader exposes a final URL with
X-Base: final but no observed redirect chain. Changed targets require the existing
renderer. Access walls are terminal. Text mode never requests generated content.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import http.client
import json
import math
from pathlib import Path
import queue
import re
import threading
import time
from urllib.parse import quote

from . import material_reader
from .comparison_card_budget import now, private_json
from .deepseek_discovery import DeepSeekSearchResponse, DiscoveryHint
from .supply_layered_sources import _public_target, rendered_source
from .supply_read_order import schedule_reads
from .supply_rendered_reader import _identity


JINA_PREFIX = 'https://r.jina.ai/'
REQUEST_HEADERS = {
    'User-Agent': 'TravelOriginalReader/1.0', 'Accept': 'application/json',
    'Accept-Encoding': 'identity', 'Connection': 'close',
    'X-Respond-With': 'text', 'X-Base': 'final', 'X-No-Cache': 'true',
    'X-With-Generated-Alt': 'false', 'X-Assert-Status-Code': '200',
    'X-With-Links-Summary': 'all',
}
_WALL = re.compile(r'robots(?:\.txt)?|forbidden|unauthorized|access denied|'
                   r'(?:error|status|code)\s*[:=]?\s*(?:401|403|451)\b', re.I)
_PAGE_WALL = re.compile(r'robots\.txt.{0,80}(?:disallow|denied|blocked)|'
                        r'(?:request|access).{0,20}(?:forbidden|unauthorized)|'
                        r'\b(?:sign|log)\s+in\s+(?:is\s+)?required\b|'
                        r'^\s*(?:error\s*:?\s*)?(?:401|403|451)\s+(?:forbidden|unauthorized|access denied)\b|'
                        r'\baccess\s+to\s+(?:this\s+|the\s+)?(?:page|site|resource)\s+is\s+denied\b',
                        re.I | re.M)
_RESPONSE_HEADERS = {'content-type', 'content-encoding', 'content-length', 'date',
                     'retry-after', 'x-request-id', 'x-usage-tokens'}


def _target(url):
    parsed = material_reader._validate_url(url)
    if parsed.hostname in {'r.jina.ai', 's.jina.ai'}:
        raise ValueError('Nested reader or search endpoints are not original sources')
    return parsed


def _fetch_jina(url, deadline, transport):
    """One bounded Jina GET through the explicit safe transport configuration."""
    from .supply_jina_transport import fetch_jina
    return fetch_jina(url, deadline, transport, REQUEST_HEADERS)


def _unavailable(capture, reason, *, terminal=False):
    capture.update(status='blocked' if terminal else 'unavailable', reason=reason,
                   fallback_forbidden=terminal, content_complete=False)
    return capture


def _decode_reply(capture, reply):
    status, headers, body, truncated = reply
    capture['raw_response_base64'] = base64.b64encode(body).decode('ascii')
    capture['body_sha256'] = hashlib.sha256(body).hexdigest()
    capture['transport'].update(http_status=status, bytes_read=len(body), truncated=truncated,
        response_headers={key: value for key, value in headers.items() if key in _RESPONSE_HEADERS})
    if status in {301, 302, 303, 307, 308}:
        return _unavailable(capture, 'jina_transport_redirect_rejected')
    if status != 200:
        return _unavailable(capture, 'jina_http_status_' + str(status),
                            terminal=status in {401, 403, 451})
    if truncated:
        return _unavailable(capture, 'jina_body_limit')
    if (headers.get('content-type', '').split(';')[0].strip().lower() not in
            {'application/json', 'text/json'} or
            headers.get('content-encoding', 'identity').lower() != 'identity'):
        return _unavailable(capture, 'jina_response_format')
    try:
        payload = json.loads(body.decode('utf-8'))
    except (ValueError, UnicodeError):
        return _unavailable(capture, 'jina_invalid_json')
    if not isinstance(payload, dict):
        return _unavailable(capture, 'jina_response_shape')
    if payload.get('code', 200) != 200:
        return _unavailable(capture, 'jina_service_error',
                            terminal=payload.get('code') in {401, 403, 451})
    data = payload.get('data', payload)
    if not isinstance(data, dict):
        return _unavailable(capture, 'jina_response_shape')
    return _interpret_page(capture, data)


def _interpret_page(capture, data):
    capture['transport']['reported_source_url'] = data.get('url')
    capture['transport']['usage'] = data.get('usage')
    warning = data.get('warning')
    if warning:
        terminal = bool(material_reader._GATE.search(str(warning)) or _WALL.search(str(warning)))
        return _unavailable(capture, 'jina_page_warning', terminal=terminal)
    try:
        final = _target(data.get('url')).geturl()
    except (ValueError, TypeError, UnicodeError):
        return _unavailable(capture, 'jina_unsafe_source_url', terminal=True)
    if final != capture['requested_url']:
        return _unavailable(capture, 'jina_unproven_source_redirect')
    text = data.get('text')  # Never substitute generated content, title or description.
    if not isinstance(text, str) or not text.strip():
        return _unavailable(capture, 'jina_original_text_missing')
    capture.update(text=text, title=data.get('title') if isinstance(data.get('title'), str) else '',
                   observed_links=copy.deepcopy(data.get('links', [])))
    if material_reader._GATE.search(text) or _PAGE_WALL.search(text):
        return _unavailable(capture, 'login_challenge_or_access_wall', terminal=True)
    provenance = _identity(text)
    if provenance.get('source_type_evidence'):
        provenance['source_type_evidence']['basis'] = 'captured publisher self-identification; uncorroborated'
    capture.update(status='read', content_complete=True, source_provenance=provenance)
    return capture


def _run_capture(capture, deadline, output):
    try:
        reply = _fetch_jina(capture['requested_url'], deadline, capture['transport'])
        # An injected test transport has no delivery hook; a response proves delivery.
        capture['transport']['request_delivery_status'] = 'response_received'
        output.put(_decode_reply(capture, reply))
    except Exception as error:
        terminal = isinstance(error, ValueError)
        capture['raw_error'] = {'type': type(error).__name__, 'message': str(error)}
        output.put(_unavailable(capture, 'jina_destination_policy' if terminal else
                                'jina_fetch_failed', terminal=terminal))
    finally:
        deadline.cancel()
        material_reader._WORKERS.release()


def capture_jina_page(url, timeout_seconds=30):
    """Return an auditable capture or failure within one wall-clock deadline."""
    from .phase1_budget_context import clamp_deadline, context_target
    if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 60):
        raise ValueError('Invalid Jina read timeout')
    capture = {'requested_url': url, 'url': url, 'text': '', 'title': '',
               'fetched_at': now(), 'status': 'unavailable', 'redirect_chain': [],
               'source_provenance': {'source_type': 'unknown'}, 'fallback_forbidden': False,
               'transport': {'provider': 'Jina Reader', 'request_url': None,
                   'method': 'GET', 'request_headers': dict(REQUEST_HEADERS),
                   'request_delivery_status': 'not_sent', 'tool_cost': 'unknown',
                   'final_url_mode': 'final', 'redirect_evidence': 'not_exposed_by_provider'}}
    try:
        target = _target(url).geturl()
    except (ValueError, TypeError, UnicodeError):
        return _unavailable(capture, 'public_url_validation_rejected', terminal=True)
    capture.update(url=target, requested_url=target)
    capture['transport']['request_url'] = JINA_PREFIX + target
    if not material_reader._WORKERS.acquire(blocking=False):
        return _unavailable(capture, 'jina_reader_busy')
    deadline, output = material_reader._ReadDeadline(), queue.Queue(maxsize=1)
    started = time.monotonic()
    deadline.end = clamp_deadline(started + timeout_seconds)
    worker = lambda: _run_capture(capture, deadline, output)
    threading.Thread(target=context_target(worker), name='jina-original-reader', daemon=True).start()
    try:
        result = output.get(timeout=max(0, deadline.end - time.monotonic()))
    except queue.Empty:
        deadline.cancel()
        result = _unavailable(copy.deepcopy(capture), 'jina_deadline_exceeded')
    result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
    result['body_length'] = len(result['text'])
    return result


def _read_schedule(urls):
    safe, excluded, upgrades = [], [], []
    for url in urls:
        try:
            target, transitions = _public_target(url)
            _target(target)
        except (ValueError, TypeError, UnicodeError) as error:
            excluded.append({'url': url, 'reason': str(error)})
            continue
        upgrades.extend(transitions)
        if target not in safe:
            safe.append(target)
    response = DeepSeekSearchResponse(tuple(safe), 'response_received',
        tuple(DiscoveryHint('Official website lead', '') for _ in safe))
    return schedule_reads(response, safe), excluded, upgrades


def _source(capture, path):
    result = rendered_source(capture)
    provenance = {'source_layer': 'official_jina', 'source_origin': 'jina_reader',
                  'capture_path': str(path), 'transport_provenance': capture.get('transport', {})}
    for row in [result['ledger'], result['source']]:
        if row is None:
            continue
        row.update(copy.deepcopy(provenance))
        for field in row.get('field_evidence', {}).values():
            field['source_layer'] = 'official_jina'
        if 'content' in row:
            row['content_scope'] = 'complete returned source text via Jina; not an exhaustive source inventory'
    return result


class JinaReader:
    """LayeredResearch-compatible callable. Bounds and official ordering are kept."""
    def __init__(self, directory, *, timeout_seconds=30, page_limit=6, capture=None,
                 identity_assessor=None):
        if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 60 or type(page_limit) is not int
                or not 1 <= page_limit <= 6):
            raise ValueError('Invalid Jina reader bounds')
        self.directory = Path(getattr(directory, 'directory', directory))
        self.timeout_seconds, self.page_limit = timeout_seconds, page_limit
        self.capture = capture or capture_jina_page
        self.identity_assessor = identity_assessor

    def __call__(self, *, seed, plan, urls, request_id):
        directory = self.directory / 'jina-captures' / hashlib.sha256(request_id.encode()).hexdigest()[:20]
        schedule, excluded, upgrades = _read_schedule(urls)
        receipt = {'request_id': request_id, 'case_id': seed['id'], 'started_at': now(),
                   'status': 'running', 'schedule': schedule, 'excluded': excluded,
                   'url_upgrades': upgrades, 'product_call_count': 0, 'tool_call_count': 0,
                   'tool_cost': 'unknown', 'captures': [], 'not_attempted': schedule[self.page_limit:]}
        private_json(directory / 'receipt.json', receipt)
        sources, ledger, fallback, blocked, captures = [], [], [], [], []
        for index, item in enumerate(schedule[:self.page_limit]):
            url = item['requested_url']
            receipt['tool_call_count'] += 1
            private_json(directory / 'receipt.json', receipt)
            capture, result = self._read(url, directory / f'capture-{index + 1}.json')
            captures.append(capture)
            receipt['captures'].append({'path': result['ledger']['capture_path'], 'url': url,
                'status': capture['status'], 'reason': capture.get('reason'),
                'body_length': len(capture.get('text', '')), 'elapsed_ms': capture.get('elapsed_ms')})
            private_json(directory / 'receipt.json', receipt)
        from .supply_jina_identity import assess_publisher_captures
        assessed = assess_publisher_captures(self, captures, directory, request_id, receipt)
        for index, capture in enumerate(assessed):
            path = directory / f'capture-{index + 1}.json'
            if capture != captures[index]:
                path = directory / f'assessed-capture-{index + 1}.json'
                private_json(path, capture)
                receipt['captures'][index]['assessed_capture_path'] = str(path)
            result = _source(capture, path)
            ledger.append(result['ledger'])
            if result['source'] is not None:
                sources.append(result['source'])
            url = capture['requested_url']
            if capture.get('fallback_forbidden'):
                blocked.append(url)
            elif not (result['source'] and result['source']['source_type'] in {'official', 'operator'}
                      and result['source']['source_type_content_bound']):
                fallback.append(url)
        receipt.update(status='read' if sources else 'failed', finished_at=now(),
                       fallback_urls=fallback, terminal_blocked_urls=blocked)
        private_json(directory / 'receipt.json', receipt)
        private_json(directory / 'source-ledger.json', ledger)
        return {'sources': sources, 'ledger': ledger, 'tool_receipt': receipt,
                'fallback_urls': fallback, 'terminal_blocked_urls': blocked}

    def _read(self, url, path):
        try:
            capture = self.capture(url, self.timeout_seconds)
            if not isinstance(capture, dict) or capture.get('requested_url') != url:
                raise ValueError('Jina capture does not match scheduled URL')
        except Exception as error:
            capture = {'requested_url': url, 'url': url, 'fetched_at': now(), 'text': '',
                       'status': 'unavailable', 'reason': 'jina_capture_failed',
                       'raw_error': {'type': type(error).__name__, 'message': str(error)}}
        private_json(path, capture)
        return capture, _source(capture, path)
