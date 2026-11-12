"""One native tool-call turn. No execution, JSON repair, retry, or fallback.

Returned assistant messages include private reasoning for protocol replay; the
runtime must persist them internally and must never publish them to the UI.
"""
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .autopilot_tokenizer import (measure_native_input, strict_json, validate_calls,
                                  validate_messages, validate_tools)
from .model import (ModelError, _model_error, _read_response, _received_usage,
                    _safe_transport_ids, _valid_timeout, settings)
from .model_boundary import normalize_json_response
from .phase1_budget_context import (BudgetExhausted, clamp_deadline, current_budget,
                                    physical_attempt, uncharged_caller)
from .writer_tokenizer import WriterTokenizerError

MAX_REQUEST_BYTES = 2_000_000
MAX_RESPONSE_BYTES = 2_000_000
_PREFLIGHTS = threading.BoundedSemaphore(4)


class _Rejected(Exception):
    def __init__(self, code, kind='config'):
        self.code, self.kind = code, kind


def _bounded_preflight(function, deadline):
    if not _PREFLIGHTS.acquire(blocking=False):
        raise _Rejected('preflight_capacity')
    output = queue.Queue(maxsize=1)

    def worker():
        try:
            output.put((True, function()))
        except Exception as error:
            output.put((False, error))
        finally:
            _PREFLIGHTS.release()

    try:
        threading.Thread(target=worker, name='native-tool-preflight', daemon=True).start()
    except RuntimeError:
        _PREFLIGHTS.release()
        raise _Rejected('preflight_start_failed') from None
    try:
        success, result = output.get(timeout=max(0, deadline - time.monotonic()))
    except queue.Empty:
        raise TimeoutError() from None
    if time.monotonic() >= deadline:
        raise TimeoutError()
    if not success:
        raise result
    return result


def _config(config, timeout, max_tokens, max_input_tokens, max_input_bytes):
    if not isinstance(config, dict):
        raise _Rejected('invalid_configuration')
    if config.get('off'):
        raise _Rejected('model_disabled')
    if not config.get('key'):
        raise _Rejected('api_key_missing')
    if not _valid_timeout(timeout):
        raise _Rejected('invalid_timeout')
    if any(type(value) is not int or value <= 0 for value in
           (max_tokens, max_input_tokens, max_input_bytes)):
        raise _Rejected('invalid_limits')
    if not all(isinstance(config.get(key), str) and config[key] for key in ('base', 'model', 'key')):
        raise _Rejected('invalid_configuration')
    endpoint = urllib.parse.urlsplit(config['base'])
    if (endpoint.scheme not in ('https', 'http') or not endpoint.hostname
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
            or '\n' in config['key'] or '\r' in config['key']):
        raise _Rejected('invalid_configuration')
    return endpoint


def _prepare(messages, tools, config, effort, max_tokens, input_limit, byte_limit, measure):
    payload = {'model': config['model'], 'messages': messages, 'tools': tools,
               'max_tokens': max_tokens, 'stream': False}
    if config['model'].startswith('deepseek-'):
        payload['thinking'] = {'type': 'disabled' if effort == 'none' else 'enabled'}
        if effort != 'none':
            payload['reasoning_effort'] = effort
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(raw) > min(byte_limit, MAX_REQUEST_BYTES):
        raise _Rejected('request_too_large')
    # Snapshot before measuring: caller mutation cannot alter the measured request.
    snapshot = strict_json(raw)
    names = validate_tools(snapshot['tools'])
    validate_messages(snapshot['messages'], names)
    facts = measure(snapshot['messages'], tools=snapshot['tools'], model=config['model'],
                    reasoning_effort=effort)
    count = facts.get('preflight_input_tokens')
    if type(count) is not int or count < 0 or facts.get('preflight_status') != 'ok':
        raise _Rejected('invalid_preflight_measurement')
    facts = {**facts, 'request_body_bytes': len(raw), 'context_input_limit': input_limit}
    if count > input_limit:
        error = _Rejected('context_input_too_large')
        error.facts = facts
        raise error
    request = urllib.request.Request(config['base'].rstrip('/') + '/chat/completions',
        data=raw, method='POST', headers={'Content-Type': 'application/json',
                                       'Authorization': 'Bearer ' + config['key']})
    previous_ids = {call['id'] for message in snapshot['messages']
                    for call in (message.get('tool_calls') or [])}
    return request, names, previous_ids, facts


def _assistant(body, names, previous_ids):
    choices = body.get('choices')
    if (not isinstance(choices, list) or len(choices) != 1
            or not isinstance(choices[0], dict)):
        raise _Rejected('invalid_response_shape', 'format')
    choice = choices[0]
    if choice.get('finish_reason') not in ('stop', 'tool_calls'):
        raise _Rejected('incomplete_response', 'format')
    message = choice.get('message')
    if not isinstance(message, dict) or message.get('role') != 'assistant':
        raise _Rejected('invalid_response_shape', 'format')
    content, reasoning = message.get('content'), message.get('reasoning_content')
    if ((content is not None and not isinstance(content, str))
            or (reasoning is not None and not isinstance(reasoning, str))):
        raise _Rejected('invalid_content_shape', 'format')
    calls = message.get('tool_calls')
    if calls:
        # Response metadata (e.g. an index) is not executable protocol state.
        # Keep the wire arguments unchanged; validation still precedes execution.
        if isinstance(calls, list):
            calls = [{**{key: call[key] for key in ('id', 'type', 'function') if key in call},
                      'function': {key: call['function'][key] for key in ('name', 'arguments')
                                   if key in call['function']}}
                     if isinstance(call, dict) and isinstance(call.get('function'), dict)
                     else call for call in calls]
        try:
            if validate_calls(calls, names) & previous_ids:
                raise _Rejected('duplicate_tool_call_id', 'format')
        except WriterTokenizerError as error:
            rejected = _Rejected('invalid_tool_calls', 'format')
            rejected.facts = {'tool_call_validation_error': error.code}
            raise rejected from None
        if choice['finish_reason'] != 'tool_calls':
            raise _Rejected('inconsistent_finish_reason', 'format')
        message = {**message, 'tool_calls': calls}
    elif calls not in (None, []) or choice['finish_reason'] != 'stop' or not content:
        raise _Rejected('invalid_content_shape', 'format')
    # Drop provider-specific extras rather than persisting opaque metadata.
    return {key: message[key] for key in ('role', 'content', 'reasoning_content', 'tool_calls')
            if key in message}


def _known_transport_facts(value, secret):
    if not isinstance(value, dict):
        return {}
    facts = {key: value[key] for key in ('input_tokens', 'output_tokens', 'reasoning_tokens')
             if type(value.get(key)) is int and value[key] >= 0}
    if 'provider_request_id' in value:
        facts.update(_safe_transport_ids({'provider_request_id': value['provider_request_id']}, secret))
    return facts


def _exchange(request, names, previous_ids, config, started, deadline, transport, facts, max_tokens):
    prepare = getattr(current_budget(), 'prepare_request', None)
    if callable(prepare):
        prepare(facts['preflight_input_tokens'], max_tokens)
    if time.monotonic() >= deadline:
        raise TimeoutError()
    with physical_attempt('model_http', max(0, deadline - time.monotonic())) as attempt:
        facts.update(physical_dispatch_started=True, physical_dispatch_status='attempted',
                     model_calls=1, request_delivery_status='unknown')
        try:
            raw = (transport(request, attempt.timeout) if transport else
                   uncharged_caller(_read_response)(request, attempt.timeout))
            transport_ids = {}
            if isinstance(raw, tuple):
                raw, receipt = raw
                safe_receipt = _known_transport_facts(receipt, config['key'])
                facts.update(safe_receipt)
                transport_ids = {key: value for key, value in safe_receipt.items()
                                 if key.startswith('provider_request_id')}
            if not isinstance(raw, bytes):
                raise _Rejected('invalid_response_shape', 'format')
            facts.update(request_delivery_status='response_received', provider_acknowledged=True)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise _Rejected('response_too_large', 'transport')
            try:
                body = strict_json(raw)
            except (ValueError, WriterTokenizerError, RecursionError):
                raise _Rejected('invalid_response_json', 'format') from None
            if not isinstance(body, dict):
                raise _Rejected('invalid_response_shape', 'format')
            receipt = _received_usage(body, config, started)
            # Never erase known transport counters when response accounting is partial.
            facts.update({key: value for key, value in receipt.items()
                          if key != 'usage_complete'})
            if transport_ids.get('provider_request_id_present'):
                for key in ('provider_request_id', 'provider_request_id_hmac_sha256'):
                    facts.pop(key, None)
                facts.update(transport_ids)
            facts['usage_complete'] = all(key in facts for key in ('input_tokens', 'output_tokens'))
            body = normalize_json_response(body, None, facts)
            result = _assistant(body, names, previous_ids)
            if time.monotonic() >= deadline:
                raise TimeoutError()
            return result
        except Exception as error:
            if isinstance(error, urllib.error.HTTPError):
                facts.update(provider_acknowledged=True, request_delivery_status='response_received')
            facts.update(_known_transport_facts(getattr(error, 'usage', {}), config['key'])
                         if getattr(error, 'usage', None) else {})
            if getattr(error, 'code', None) in ('transport_capacity', 'transport_start_failed'):
                facts.update(physical_dispatch_started=False, physical_dispatch_status='not_sent',
                             model_calls=0, request_delivery_status='not_sent')
            facts['usage_complete'] = all(key in facts for key in ('input_tokens', 'output_tokens'))
            error.usage = dict(facts)
            raise
        finally:
            attempt.usage = dict(facts)


def call_tools(messages, *, tools, timeout=30, max_tokens=None,
               max_input_tokens=32768, max_input_bytes=MAX_REQUEST_BYTES,
               reasoning_effort=None, transport=None, config=None, measure=None):
    """Return (native assistant message, usage), or ModelError with safe evidence.

    transport(request, timeout) -> bytes or (bytes, safe receipt). Test-only
    measure injection requires an explicit transport and a .invalid base host.
    Production always measures with the pinned encoder, including full tools.
    """
    from .model_sizing import stage_limit
    if max_tokens is None:
        max_tokens = stage_limit('native_tools')
    started = time.monotonic()
    config = settings() if config is None else config
    facts = {'usage_complete': False, 'model_calls': 0, 'physical_dispatch_started': False,
             'physical_dispatch_status': 'not_sent', 'request_delivery_status': 'not_sent',
             'provider_acknowledged': None}
    try:
        if not isinstance(config, dict):
            config = {}
            raise _Rejected('invalid_configuration')
        config = dict(config)
        endpoint = _config(config, timeout, max_tokens, max_input_tokens, max_input_bytes)
        if measure is not None and (transport is None or not endpoint.hostname.endswith('.invalid')):
            raise _Rejected('test_measure_requires_fake_transport')
        effort = reasoning_effort if reasoning_effort is not None else os.getenv('TRAVEL_AGENT_REASONING', 'none')
        deadline = clamp_deadline(started + timeout)
        request, names, previous_ids, preflight = _bounded_preflight(lambda: _prepare(
            messages, tools, config, effort, max_tokens, max_input_tokens,
            max_input_bytes, measure or measure_native_input), deadline)
        facts.update(preflight)
        if time.monotonic() >= deadline:
            raise TimeoutError()
        result = _exchange(request, names, previous_ids, config, started, deadline, transport, facts, max_tokens)
        return result, facts
    except Exception as error:
        facts.update(getattr(error, 'facts', {}))
        if isinstance(error, _Rejected):
            kind, code = error.kind, error.code
        elif isinstance(error, WriterTokenizerError):
            facts.update(error.safe_facts)
            kind, code = 'config', 'native_tokenizer_unavailable'
        elif isinstance(error, urllib.error.HTTPError):
            kind, code = 'http', 'http_error'
            diagnostics = getattr(error, '_travel_diagnostic', {})
            facts.update(_known_transport_facts(diagnostics, config.get('key')))
            facts.update(provider_acknowledged=True, request_delivery_status='response_received')
            error.close()
        elif isinstance(error, BudgetExhausted):
            # Admission stops carry a machine reason, not a transport timeout.
            # Never expose arbitrary exception text as a public error code.
            reason = getattr(error, 'code', None)
            code = (reason if isinstance(reason, str) and
                    re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,99}', reason) else 'BUDGET_EXCEEDED')
            kind = 'budget'
        elif isinstance(error, TimeoutError) or (
                isinstance(error, urllib.error.URLError) and isinstance(error.reason, TimeoutError)):
            kind, code = 'timeout', 'request_timeout'
            facts['timeout_stage'] = 'transport' if facts['physical_dispatch_started'] else 'preflight'
        elif isinstance(error, OSError):
            kind, code = 'network', 'network_error'
        else:
            kind, code = ('format', 'invalid_response_shape') if facts['physical_dispatch_started'] else ('config', 'request_encoding')
        raise _model_error('Native tool call failed: ' + code, kind, code, config, started,
                           facts, http_status=getattr(error, 'code', None) if kind == 'http' else None,
                           transport_error=kind in ('network', 'timeout', 'http')) from None
