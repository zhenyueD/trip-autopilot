"""Bounded Jina HTTPS transport with an explicit credential-free loopback proxy.

Proxy mode uses DNS-over-HTTPS through that proxy, pinned to Google's public DNS
bootstrap address with normal TLS hostname verification. All returned A/AAAA
addresses must satisfy the existing public-destination policy. No local/fake IP
is accepted as an original or remote endpoint; proxy loopback is transport only.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import socket
import ssl
import time
from urllib.parse import quote, urlencode, urlsplit

from . import material_reader
from .phase1_budget_context import physical_attempt

DNS_HOST = 'dns.google'
DNS_IP = '8.8.8.8'
DNS_MAX_BYTES = 32768
JINA_HOST = 'r.jina.ai'


def _stage(record, stage, host):
    record.update(stage=stage, stage_host=host)
    record.setdefault('stages', []).append({'stage': stage, 'host': host})


def _failure(record):
    record.setdefault('failure_stage', record.get('stage'))
    record.setdefault('failure_host', record.get('stage_host'))


def _proxy():
    values = [os.environ[key] for key in ('HTTPS_PROXY', 'https_proxy') if os.environ.get(key)]
    if not values:
        return None
    if len(set(values)) != 1:
        raise ValueError('Conflicting explicit HTTPS proxy configurations')
    try:
        parsed = urlsplit(values[0])
        valid = (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', '::1'}
                 and parsed.port is not None and 1 <= parsed.port <= 65535
                 and parsed.username is None and parsed.password is None
                 and parsed.path in {'', '/'} and not parsed.query and not parsed.fragment)
    except (ValueError, UnicodeError):
        valid = False
    if not valid:
        raise ValueError('HTTPS proxy must be an explicit credential-free loopback HTTP endpoint')
    return parsed.hostname, parsed.port


def _local_addresses(host, receipt=None):
    answers = socket.getaddrinfo(host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
    if receipt is not None:
        receipt['observed_addresses'] = [row[4][0] for row in answers]
    if not answers or len(answers) > 64:
        raise ValueError('Public host resolution is unavailable')
    addresses = []
    for family, _, _, _, address in answers:
        if family not in {socket.AF_INET, socket.AF_INET6} or not material_reader._public_ip(address[0]):
            raise ValueError('DNS includes a non-public destination')
        if address[0] not in addresses:
            addresses.append(address[0])
    return addresses


def _dns_addresses(payload, host, kind):
    if (not isinstance(payload, dict) or payload.get('Status') != 0
            or payload.get('TC') is True or not isinstance(payload.get('Question'), list)
            or len(payload['Question']) != 1):
        raise ValueError('Trusted DNS response is incomplete or failed')
    question = payload['Question'][0]
    if question != {'name': host + '.', 'type': kind} and question != {'name': host, 'type': kind}:
        raise ValueError('Trusted DNS question does not match requested host/type')
    answers = payload.get('Answer', [])
    if not isinstance(answers, list) or len(answers) > 64:
        raise ValueError('Trusted DNS answer count is invalid')
    addresses = []
    for answer in answers:
        if not isinstance(answer, dict):
            raise ValueError('Trusted DNS answer shape is invalid')
        value, answer_kind = answer.get('data'), answer.get('type')
        if answer_kind == 5:
            material_reader._validate_host(str(value).rstrip('.'))
            continue
        if answer_kind not in {1, 28} or not isinstance(value, str):
            raise ValueError('Trusted DNS answer type is unsupported')
        address = ipaddress.ip_address(value)
        if address.version != (4 if answer_kind == 1 else 6) or not material_reader._public_ip(value):
            raise ValueError('Trusted DNS includes a non-public or invalid destination')
        addresses.append(str(address))
    return addresses


def _tunnel(sock, address, deadline, record):
    _stage(record, 'proxy_connect_request', record['stage_host'])
    target = '[' + address + ']' if ':' in address else address
    connection = http.client.HTTPConnection('unused', 443)
    connection.set_tunnel(target, 443)
    connection.sock = sock
    sock.settimeout(deadline.remaining())
    connection._tunnel()  # Standard bounded HTTP header parser; one CONNECT, no redirect.
    record['proxy_connect_status'] = 200
    connection.sock = None  # Caller owns this socket and the cancellation handle.


def _tls_socket(host, address, proxy, deadline, record):
    if not material_reader._public_ip(address):
        raise ValueError('Remote TLS destination must be public')
    target = proxy if proxy else (address, 443)
    family = socket.AF_INET6 if ':' in target[0] else socket.AF_INET
    _stage(record, 'proxy_tcp_connect' if proxy else 'tcp_connect', host)
    raw = socket.socket(family, socket.SOCK_STREAM)
    live = raw
    deadline.track(raw)
    try:
        raw.settimeout(deadline.remaining())
        raw.connect(target)
        if proxy:
            _tunnel(raw, address, deadline, record)
        _stage(record, 'tls_handshake', host)
        context = ssl.create_default_context()
        live = context.wrap_socket(raw, server_hostname=host, do_handshake_on_connect=False)
        deadline.track(live)
        live.settimeout(deadline.remaining())
        live.do_handshake()
        return live
    except Exception:
        live.close()
        raise


def _get(host, path, address, proxy, deadline, headers, record):
    connection, sock = None, None
    try:
        with physical_attempt('public_page_http', max(0, deadline.end - time.monotonic())):
            sock = _tls_socket(host, address, proxy, deadline, record)
            connection = http.client.HTTPConnection(host, 443)
            connection.sock = sock
            _stage(record, 'http_send', host)
            record['request_delivery_status'] = 'unknown'
            sock.settimeout(deadline.remaining())
            connection.request('GET', path, headers=headers)
            record['request_delivery_status'] = 'sent'
            _stage(record, 'http_response_headers', host)
            sock.settimeout(deadline.remaining())
            response = connection.getresponse()
            record.update(request_delivery_status='response_received', http_status=response.status)
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            _stage(record, 'http_response_body', host)
            body, truncated = material_reader._read_body(response, sock, deadline)
            record.update(bytes_read=len(body), truncated=truncated)
            return response.status, response_headers, body, truncated
    except Exception:
        _failure(record)
        raise
    finally:
        if connection is not None:
            connection.close()
        elif sock is not None:
            sock.close()


def _dns_query(host, kind, proxy, deadline, record):
    query = {'host': host, 'type': kind, 'resolver': DNS_HOST, 'bootstrap_ip': DNS_IP,
             'request_delivery_status': 'not_sent'}
    record.setdefault('dns_queries', []).append(query)
    _stage(record, 'trusted_dns_query', DNS_HOST)
    try:
        reply = _get(DNS_HOST, '/resolve?' + urlencode({'name': host, 'type': kind}),
                     DNS_IP, proxy, deadline,
                     {'Accept': 'application/dns-json', 'Accept-Encoding': 'identity',
                      'Connection': 'close'}, query)
        status, headers, body, truncated = reply
        query['body_sha256'] = hashlib.sha256(body).hexdigest()
        if (status != 200 or truncated or len(body) > DNS_MAX_BYTES
                or headers.get('content-encoding', 'identity').lower() != 'identity'
                or headers.get('content-type', '').split(';')[0].strip().lower()
                not in {'application/json', 'application/dns-json'}):
            raise ValueError('Trusted DNS HTTP response is unusable')
        payload = json.loads(body.decode('utf-8'))
        query['response'] = payload
        _stage(record, 'trusted_dns_validate', host)
        return _dns_addresses(payload, host, kind)
    except Exception:
        if record['stage'] == 'trusted_dns_validate':
            query['failure_stage'] = 'trusted_dns_validate'
            record.setdefault('failure_stage', 'trusted_dns_validate')
            record.setdefault('failure_host', host)
        else:
            query.setdefault('failure_stage', query.get('stage', 'query'))
            record.setdefault('failure_stage', 'trusted_dns_' + query['failure_stage'])
            record.setdefault('failure_host', query.get('stage_host', DNS_HOST))
        raise


def _addresses(host, proxy, deadline, record):
    _stage(record, 'trusted_dns' if proxy else 'local_dns', host)
    receipt = {'host': host, 'resolver': DNS_HOST if proxy else 'system', 'addresses': []}
    record.setdefault('dns', []).append(receipt)
    if proxy:
        values = [value for kind in (1, 28)
                  for value in _dns_query(host, kind, proxy, deadline, record)]
    else:
        values = _local_addresses(host, receipt)
    deadline.remaining()
    if not values:
        raise ValueError('Public DNS returned no usable addresses')
    receipt.update(addresses=list(dict.fromkeys(values)), validated_public=True)
    return receipt['addresses']


def fetch_jina(url, deadline, record, headers):
    """Fetch one original via fixed Jina HTTPS, with separately accounted DNS reads."""
    try:
        _stage(record, 'url_validation', None)
        original = material_reader._validate_url(url)
        if original.hostname in {'r.jina.ai', 's.jina.ai'}:
            raise ValueError('Nested readers are not original sources')
        _stage(record, 'proxy_configuration', None)
        proxy = _proxy()
        record.update(mode='explicit_loopback_proxy' if proxy else 'direct',
                      proxy={'host': proxy[0], 'port': proxy[1]} if proxy else None)
        _addresses(original.hostname, proxy, deadline, record)
        address = _addresses(JINA_HOST, proxy, deadline, record)[0]
        endpoint = material_reader._validate_url('https://' + JINA_HOST + '/' + original.geturl())
        path = quote(endpoint.path, safe="/:@!$&'()*+,;=-._~")
        query = quote(endpoint.query, safe="=&?/:@!$'()*+,;~-._")
        record['wire_request_target'] = path + ('?' + query if query else '')
        return _get(JINA_HOST, record['wire_request_target'], address, proxy, deadline, headers, record)
    except Exception:
        _failure(record)
        raise
