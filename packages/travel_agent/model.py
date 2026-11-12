"""OpenAI-compatible JSON/vision calls, with explicit failures and usage evidence.

Extends the project's stdlib HTTP-client approach without changing legacy narration.
Provider endpoints and credentials are server configuration, never model/user URLs.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import queue
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .material_reader import _ReadDeadline
from .model_boundary import normalize_json_response
from .writer_tokenizer import (WriterTokenizerError, measure_writer_input,
                               measure_writer_request_size)
from .writer_context import ContextAdmissionError, enforce_preflight_budget
from .writer_transport import (_REQUEST_ID_HEADERS as _WRITER_REQUEST_ID_HEADERS,
                               WriterStreamError, WriterTimeoutConfigError,
                               read_stream_response as _read_writer_stream)

# DNS cannot be interrupted portably. Late opens are discarded, and this cap
# prevents stalled OS/provider work from accumulating unlimited daemon threads.
_MODEL_TRANSPORTS = threading.BoundedSemaphore(8)
_WRITER_PREFLIGHTS = threading.BoundedSemaphore(4)
_REQUEST_ID_HMAC_KEY = secrets.token_bytes(32)
_HTTP_ERROR_BODY_LIMIT = 2048
_HTTP_DIAGNOSTIC_HEADERS = {'retry-after'}
_SENSITIVE_ERROR_KEYS = re.compile(
    r'(?:api[-_]?key|authorization|credential|password|secret|token|cookie)', re.I)
_BEARER_SECRET = re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+')
_API_KEY_SECRET = re.compile(r'\bsk-[A-Za-z0-9_-]+')
MODEL_ERROR_CODES = {
    'config': frozenset({'writer_model_missing', 'writer_key_missing', 'model_disabled',
                         'api_key_missing', 'invalid_configuration', 'invalid_timeout',
                         'request_encoding', 'writer_tokenizer_unavailable',
                         'context_input_too_large'}),
    'timeout': frozenset({'request_timeout'}),
    'network': frozenset({'network_error', 'transport_capacity', 'transport_start_failed'}),
    'http': frozenset({'http_error'}),
    'transport': frozenset({'response_too_large'}),
    'format': frozenset({'invalid_response_json',
                         'invalid_response_shape', 'invalid_content_json',
                         'invalid_content_shape', 'incomplete_response'}),
}


class _TransportUnavailable(OSError):
    def __init__(self, code):
        super().__init__('Model transport unavailable')
        self.code = code


class _WriterPreflightTimeout(TimeoutError):
    """The local official-tokenizer preflight exhausted the call deadline."""


def _bounded_writer_preflight(messages, *, model, reasoning_effort, deadline):
    """Run local tokenization under the same hard wall clock as the call."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _WriterPreflightTimeout('Writer preflight deadline')
    if not _WRITER_PREFLIGHTS.acquire(blocking=False):
        raise WriterTokenizerError('preflight_capacity')
    output = queue.Queue(maxsize=1)

    def worker():
        try:
            output.put((True, measure_writer_input(
                messages, model=model, reasoning_effort=reasoning_effort)))
        except WriterTokenizerError as error:
            output.put((False, error))
        except Exception:
            output.put((False, WriterTokenizerError('tokenization_failed')))
        finally:
            _WRITER_PREFLIGHTS.release()

    try:
        threading.Thread(target=worker, name='writer-tokenizer-preflight',
                         daemon=True).start()
    except RuntimeError:
        _WRITER_PREFLIGHTS.release()
        raise WriterTokenizerError('preflight_start_failed') from None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _WriterPreflightTimeout('Writer preflight deadline')
    try:
        success, value = output.get(timeout=remaining)
    except queue.Empty:
        raise _WriterPreflightTimeout('Writer preflight deadline') from None
    if time.monotonic() >= deadline:
        raise _WriterPreflightTimeout('Writer preflight deadline')
    if not success:
        raise value
    return value


def _limit_utf8(value, limit=_HTTP_ERROR_BODY_LIMIT):
    raw = str(value).encode('utf-8')
    if len(raw) <= limit:
        return str(value)
    return raw[:limit].decode('utf-8', errors='ignore')


def _redact_error_value(value):
    if isinstance(value, dict):
        return {key: ('<REDACTED>' if _SENSITIVE_ERROR_KEYS.search(str(key))
                      else _redact_error_value(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_error_value(item) for item in value]
    if isinstance(value, str):
        return _API_KEY_SECRET.sub('<REDACTED>',
               _BEARER_SECRET.sub('Bearer <REDACTED>', value))
    return value


def _sanitize_http_error_body(raw):
    text = raw.decode('utf-8', errors='replace')
    try:
        value = _redact_error_value(json.loads(text))
        text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    except (TypeError, ValueError):
        text = _redact_error_value(text)
    return _limit_utf8(text)


def _request_id_evidence(value, configured_secret=None):
    """Keep bounded provider correlation IDs, rejecting credentials and free text."""
    present = isinstance(value, str) and bool(value)
    result = {'provider_request_id_present': present}
    if (present and len(value) <= 256 and value.isascii()
            and not (isinstance(configured_secret, str) and configured_secret
                     and configured_secret in value)):
        if (re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,255}', value)
                and not _API_KEY_SECRET.search(value)
                and not any(marker in value.lower() for marker in ('secret', 'api_key', 'authorization'))):
            result['provider_request_id'] = value
        result['provider_request_id_hmac_sha256'] = hmac.new(
            _REQUEST_ID_HMAC_KEY, value.encode('ascii'), hashlib.sha256).hexdigest()
    return result


def _capture_http_error(error, secret=None):
    """Capture bounded, redacted diagnostics before HTTPError closes its body."""
    body_status = 'complete'
    try:
        raw = error.read(_HTTP_ERROR_BODY_LIMIT + 1)
    except TimeoutError:
        body_status = 'timeout'
        raw = b''
    except (OSError, ValueError):
        body_status = 'error'
        raw = b''
    if not isinstance(raw, bytes):
        raw = str(raw or '').encode('utf-8', errors='replace')
    truncated = len(raw) > _HTTP_ERROR_BODY_LIMIT
    raw = _redact_configured_secret(raw, secret, truncated_tail=truncated)
    headers = {}
    request_id_evidence = {}
    normalized_headers = {
        str(key).lower(): str(value)
        for key, value in (error.headers.items() if error.headers else [])
    }
    for name in _WRITER_REQUEST_ID_HEADERS:
        value = normalized_headers.get(name)
        if value:
            request_id_evidence = _request_id_evidence(value, secret)
            break
    for normalized, value in normalized_headers.items():
        if normalized in _WRITER_REQUEST_ID_HEADERS:
            continue
        if normalized in _HTTP_DIAGNOSTIC_HEADERS or normalized.startswith('x-ratelimit-'):
            headers[normalized] = _limit_utf8(
                _redact_error_value(_redact_configured_secret(value, secret)), limit=512)
    return {
        'http_status': error.code,
        'provider_error_body': _sanitize_http_error_body(raw[:_HTTP_ERROR_BODY_LIMIT]),
        'provider_error_body_truncated': truncated,
        'provider_error_headers': headers,
        'provider_error_body_status': body_status,
        **request_id_evidence,
    }


def _redact_configured_secret(value, secret, *, truncated_tail=False):
    """Provider errors can echo any configured key, including JSON-escaped text."""
    if not isinstance(secret, str) or not secret:
        return value
    if isinstance(value, dict):
        return {_redact_configured_secret(key, secret): _redact_configured_secret(item, secret)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_configured_secret(item, secret) for item in value]
    if isinstance(value, (str, bytes)):
        forms = {secret, json.dumps(secret, ensure_ascii=False)[1:-1],
                 json.dumps(secret, ensure_ascii=True)[1:-1]}
        replacement = '<REDACTED>'
        if isinstance(value, bytes):
            forms = {form.encode('utf-8') for form in forms}
            replacement = replacement.encode()
        for form in sorted(forms, key=len, reverse=True):
            value = value.replace(form, replacement)
            if truncated_tail:
                # The bounded read may end inside a key; never expose its prefix.
                for size in range(min(len(form) - 1, len(value)), 0, -1):
                    if value.endswith(form[:size]):
                        value = value[:-size] + replacement
                        break
    return value


def _deadline_context(deadline):
    """Keep stdlib certificate checks while tracking TLS before handshake/read."""
    context = ssl.create_default_context()
    context.set_alpn_protocols(['http/1.1'])
    original = context.wrap_socket

    def wrap_socket(sock, *args, **kwargs):
        deadline.remaining()
        kwargs['do_handshake_on_connect'] = False
        secured = original(sock, *args, **kwargs)
        deadline.track(secured)
        secured.settimeout(deadline.remaining())
        secured.do_handshake()
        return secured

    context.wrap_socket = wrap_socket
    return context


def _read_response(request, timeout):
    from .phase1_budget_context import current_budget, physical_attempt, transport_usage
    prepare = getattr(current_budget(), 'prepare_http_request', None)
    trace = _response_diagnostics(request)
    request._travel_response_facts = None
    try:
        if callable(prepare):
            prepare(request)
        with physical_attempt('model_http', timeout) as attempt:
            try:
                trace.stage('transport')
                raw = _read_response_unbudgeted(request, attempt.timeout)
                trace.stage('response-read')
                trace.inherit_response_id(getattr(request, '_travel_response_facts', None))
                trace.usage(transport_usage(raw))
                if attempt.lease is not None:
                    parse_usage = getattr(current_budget(), 'transport_usage', None)
                    attempt.usage = (parse_usage(request, raw) if callable(prepare) and callable(parse_usage)
                                     else transport_usage(raw))
                    trace.usage(attempt.usage)
            except Exception as error:
                # Annotate before physical_attempt calls lease.fail, not afterward.
                if not getattr(error, '_travel_nonstream_facts', False):
                    raise trace.publish(error)
                raise
        request._travel_response_facts = trace.finish()
        return raw
    except Exception as error:
        if not getattr(error, '_travel_nonstream_facts', False):
            raise trace.publish(error)
        raise


def _response_diagnostics(request):
    from .model_response_diagnostics import ResponseDiagnostics
    return ResponseDiagnostics(request, _WRITER_REQUEST_ID_HEADERS, _request_id_evidence)


def _read_response_unbudgeted(request, timeout):
    """One wall-clock budget including open/read, not a resettable socket wait.

    The caller cancels the live socket; only its worker closes the response file,
    because closing a buffered file from another thread can wait on its I/O lock.
    No late response can become a plan, even when OS resolution outlives the call.
    """
    trace = _response_diagnostics(request)
    request._travel_response_facts = None
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise trace.publish(TimeoutError('Invalid model request deadline'))
    deadline, output = _ReadDeadline(), queue.Queue(maxsize=1)
    deadline.end = time.monotonic() + timeout
    transports = _MODEL_TRANSPORTS
    if not transports.acquire(blocking=False):
        raise trace.publish(_TransportUnavailable('transport_capacity'))

    def worker():
        try:
            context = _deadline_context(deadline)
            from .phase1_budget_context import open_model_response, transport_usage
            remaining = deadline.remaining()
            trace.stage('transport')
            response = open_model_response(request, remaining, context)
            trace.stage('response-read', response)
            with response:
                # HTTP/local providers do not enter the TLS wrapper above.
                sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                if sock is not None:
                    deadline.track(sock)
                deadline.remaining()
                raw = response.read(2_000_001)
                trace.usage(transport_usage(raw))
                deadline.remaining()
            output.put((True, raw))
        except Exception as error:
            if isinstance(error, urllib.error.HTTPError):
                trace.stage('response-read', error)
                # Latch the HTTP failure before optional body diagnostics can stall.
                trace.fail(error, {'http_status': error.code, 'provider_error_body': '',
                    'provider_error_body_truncated': False, 'provider_error_headers': {},
                    'provider_error_body_status': 'not_read'})
                try:
                    diagnostic = _capture_http_error(error, trace.secret)
                    for secret in trace.secrets:
                        diagnostic = _redact_configured_secret(diagnostic, secret)
                    trace.fail(error, _safe_transport_ids(diagnostic, trace.secret))
                except Exception:
                    pass
                finally:
                    try:
                        error.close()
                    except Exception:
                        pass
            else:
                trace.fail(error)
            output.put((False, error))
        finally:
            deadline.cancel()
            transports.release()

    try:
        from .phase1_budget_context import context_target
        threading.Thread(target=context_target(worker), name='model-transport', daemon=True).start()
    except RuntimeError:
        transports.release()
        deadline.cancel()
        raise trace.publish(_TransportUnavailable('transport_start_failed')) from None
    try:
        success, value = output.get(timeout=max(0, deadline.end - time.monotonic()))
        if time.monotonic() >= deadline.end:
            raise trace.publish(TimeoutError('Model request deadline'))
        if not success:
            raise trace.publish(value)
        request._travel_response_facts = trace.finish()
        return value
    except queue.Empty:
        raise trace.publish(TimeoutError('Model request deadline')) from None
    finally:
        deadline.cancel()


class ModelError(Exception):
    def __init__(self, message, usage=None, *, format_error=False, transport_error=False):
        super().__init__(message)
        self.usage = usage or {}
        self.format_error = format_error
        self.transport_error = transport_error


def _safe_model(value, key=None):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,200}', value)
            or '://' in value or _API_KEY_SECRET.search(value)
            or (isinstance(key, str) and key and key in value)):
        return None
    return value


def _model_error(message, kind, code, config, started, usage=None, *,
                 http_status=None, format_error=False, transport_error=False):
    facts = dict(usage or {})
    facts.update(error_type=kind, error_code=code, http_status=http_status,
                 model=_safe_model(facts.get('model') or config.get('model'), config.get('key')),
                 elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
                 usage_complete=facts.get('usage_complete') is True)
    error = ModelError(message, facts, format_error=format_error, transport_error=transport_error)
    error.http_status = http_status
    return error


def _valid_timeout(timeout):
    return type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0


def settings(vision=False, *, environ=None):
    getenv = os.getenv if environ is None else environ.get
    if vision:
        google = bool(getenv('GOOGLE_API_KEY')) and not getenv('TRAVEL_VISION_API_KEY')
        return {
            'key': getenv('TRAVEL_VISION_API_KEY') or (getenv('GOOGLE_API_KEY') if google else getenv('OPENAI_API_KEY')),
            'base': getenv('TRAVEL_VISION_BASE_URL') or ('https://generativelanguage.googleapis.com/v1beta/openai' if google else getenv('OPENAI_BASE_URL', 'https://api.openai.com/v1')),
            'model': getenv('TRAVEL_VISION_MODEL') or ('gemini-2.5-flash' if google else 'gpt-4.1-mini'),
            'off': getenv('TRAVEL_AGENT_VISION_OFF') == '1',
        }
    explicit_key = getenv('TRAVEL_AGENT_API_KEY')
    provider = getenv('TRAVEL_AGENT_PROVIDER', 'auto').lower()
    google = bool(getenv('GOOGLE_API_KEY')) and provider in {'auto', 'google', 'gemini'}
    deepseek = bool(getenv('DEEPSEEK_API_KEY'))
    if not explicit_key and google:
        return {
            'key': getenv('GOOGLE_API_KEY'),
            'base': getenv('TRAVEL_AGENT_BASE_URL', 'https://generativelanguage.googleapis.com/v1beta/openai'),
            'model': getenv('TRAVEL_AGENT_MODEL', 'gemini-2.5-flash'),
            'off': getenv('ATOM_LLM_OFF') == '1',
        }
    return {
        'key': explicit_key or getenv('DEEPSEEK_API_KEY') or getenv('OPENAI_API_KEY'),
        'base': getenv('TRAVEL_AGENT_BASE_URL') or (getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com') if deepseek else getenv('OPENAI_BASE_URL', 'https://api.openai.com/v1')),
        'model': getenv('TRAVEL_AGENT_MODEL') or (getenv('DEEPSEEK_MODEL', 'deepseek-v4-pro') if deepseek else getenv('OPENAI_MODEL', 'gpt-4.1-mini')),
        'off': getenv('ATOM_LLM_OFF') == '1',
    }


def capabilities():
    planner, vision = settings(), settings(True)
    return {'planner_available': bool(planner['key']) and not planner['off'],
            'vision_available': bool(vision['key']) and not vision['off'],
            'planner_model': planner['model'], 'vision_model': vision['model'],
            'planner_fallback_model': vision['model'] if vision['key'] and not vision['off'] else None,
            'workflow': 'atomic_journey',
            'scope': '开放目的地旅行准备 · 本机预览',
            'supply_mode': '模型驱动开放地点发现、来源图片、体验内容与真实路网参考；未接实时库存或自动下单'}


def writer_settings(*, environ=None):
    """Isolated writer overrides; never send a critic key to another provider."""
    started = time.monotonic()
    config = settings(False) if environ is None else settings(False, environ=environ)
    getenv = os.getenv if environ is None else environ.get
    model = getenv('TRAVEL_WRITER_MODEL')
    base = getenv('TRAVEL_WRITER_BASE_URL')
    key = getenv('TRAVEL_WRITER_API_KEY')
    if not any((model, base, key)):
        return config
    if not model:
        raise _model_error('Writer模型配置缺失，未发送请求', 'config',
                           'writer_model_missing', {'model': None}, started)
    if base and base.rstrip('/') != config['base'].rstrip('/') and not key:
        raise _model_error('Writer独立服务缺少专用密钥，未发送请求', 'config',
                           'writer_key_missing', {**config, 'model': model}, started)
    return {**config, 'model': model, 'base': base or config['base'],
            'key': key or config['key']}


def _model_request(messages, config, started, max_tokens, reasoning_effort, response_schema,
                   stream=False):
    payload = {'model': config['model'], 'messages': messages, 'temperature': 0.35,
               'max_tokens': max_tokens, 'response_format': {'type': 'json_object'}}
    if (response_schema is not None and config['base'].rstrip('/') == 'https://api.openai.com/v1'
            and config['model'].startswith('gpt-4.1')):
        # The plan deliberately admits existing extension fields. Strict mode
        # would close every object and require nullable optional keys, changing
        # that contract. Supply the full open schema without claiming a hard
        # decoding guarantee; the existing local validator remains authoritative.
        payload['response_format'] = {'type': 'json_schema', 'json_schema': {
            'name': 'writer_plan', 'strict': False, 'schema': response_schema}}
    if config['model'].startswith('deepseek-'):
        effort = reasoning_effort or os.getenv('TRAVEL_AGENT_REASONING', 'none')
        payload['thinking'] = {'type': 'disabled' if effort == 'none' else 'enabled'}
        if effort != 'none':
            payload['reasoning_effort'] = effort
    elif config['model'].startswith('gemini-2.5-'):
        # Gemini's documented OpenAI compatibility maps medium to a bounded
        # thinking budget. Keep fast discovery, but allow actual plan reasoning.
        # https://ai.google.dev/gemini-api/docs/openai#thinking
        payload['reasoning_effort'] = reasoning_effort or 'none'
    if stream:
        payload['stream'] = True
        payload['stream_options'] = {'include_usage': True}
    try:
        encoded = json.dumps(payload, ensure_ascii=False).encode()
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise _model_error('模型请求无法编码，未发送请求', 'config', 'request_encoding',
                           config, started) from None
    return urllib.request.Request(config['base'].rstrip('/') + '/chat/completions',
        data=encoded, method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + config['key']})


def _safe_transport_ids(metrics, configured_secret):
    """Validate provider correlation IDs before they leave the transport boundary."""
    if not isinstance(metrics, dict):
        return {}
    result = dict(metrics)
    if 'provider_request_id' in result:
        request_id = result.pop('provider_request_id')
        result.update(_request_id_evidence(request_id, configured_secret))
    attempts = result.get('transport_attempts')
    if isinstance(attempts, list):
        result['transport_attempts'] = [
            _safe_transport_ids(attempt, configured_secret)
            for attempt in attempts if isinstance(attempt, dict)]
    return result


def _transport_error_usage(error, configured_secret=None):
    """Extract only the already-sanitized transport facts from wrapped errors."""
    sources = [getattr(error, 'usage', None),
               getattr(error, '_writer_transport_usage', None)]
    reason = getattr(error, 'reason', None)
    sources.extend((getattr(reason, 'usage', None),
                    getattr(reason, '_writer_transport_usage', None)))
    result = {}
    for value in sources:
        if isinstance(value, dict):
            result.update(value)
    return _safe_transport_ids(result, configured_secret)


def _stream_facts(preflight, transport, configured_secret=None):
    transport = _safe_transport_ids(transport, configured_secret)
    # Call-level preflight facts own the full deadline. Transport keeps its
    # narrower post-tokenization budget in transport_timeout_ms.
    facts = {**transport, **preflight}
    attempts = transport.get('transport_attempts')
    if isinstance(attempts, list):
        facts['transport_attempts'] = [
            {**attempt, **preflight} for attempt in attempts if isinstance(attempt, dict)]
    return facts


def _configured_request(messages, vision, model_role, started, timeout, options):
    config = writer_settings() if model_role == 'writer' and not vision else settings(vision)
    if config.get('off') or not config.get('key'):
        code = 'model_disabled' if config.get('off') else 'api_key_missing'
        raise _model_error('视觉模型未配置' if vision else '规划模型未配置或已关闭',
                           'config', code, config, started)
    if not _valid_timeout(timeout):
        raise _model_error('模型请求时限配置无效，未发送请求', 'config', 'invalid_timeout',
                           config, started)
    try:
        if not all(isinstance(config.get(key), str) and config[key] for key in ('base', 'model', 'key')):
            raise ValueError()
        endpoint = urllib.parse.urlsplit(config['base'])
        if endpoint.scheme not in ('http', 'https') or not endpoint.hostname:
            raise ValueError()
        request = _model_request(messages, config, started, **options)
    except (TypeError, ValueError, AttributeError):
        raise _model_error('模型服务配置无效，未发送请求', 'config',
                           'invalid_configuration', config, started) from None
    return config, request


def _received_usage(body, config, started):
    usage = body.get('usage')
    usage = usage if isinstance(usage, dict) else {}
    elapsed = max(0, round((time.monotonic() - started) * 1000))
    metrics = {'model': _safe_model(body.get('model'), config.get('key'))
                        or _safe_model(config['model'], config.get('key')),
               'model_calls': 1, 'duration_ms': elapsed, 'elapsed_ms': elapsed}
    for source, target in (('prompt_tokens', 'input_tokens'), ('completion_tokens', 'output_tokens')):
        value = usage.get(source)
        if type(value) is int and value >= 0:
            metrics[target] = value
    details = usage.get('completion_tokens_details')
    reasoning = details.get('reasoning_tokens') if isinstance(details, dict) else None
    if type(reasoning) is int and reasoning >= 0:
        # A subset of output_tokens, never added again to billed totals.
        metrics['reasoning_tokens'] = reasoning
    metrics['usage_complete'] = all(key in metrics for key in ('input_tokens', 'output_tokens'))
    metrics.update(_request_id_evidence(body.get('id'), config.get('key')))
    return metrics


def _response_content(body, config, started):
    if not isinstance(body, dict):
        raise _model_error('模型返回内容不完整，未覆盖已有方案', 'format',
                           'invalid_response_shape', config, started, format_error=True)
    metrics = _received_usage(body, config, started)
    choices = body.get('choices')
    if (not isinstance(choices, list) or not choices or not isinstance(choices[0], dict)
            or not isinstance(choices[0].get('message'), dict)):
        raise _model_error('模型返回内容不完整，未覆盖已有方案', 'format',
                           'invalid_response_shape', config, started, metrics, format_error=True)
    choice = choices[0]
    reason = choice.get('finish_reason')
    metrics['finish_reason'] = reason if reason in (
        'stop', 'length', 'tool_calls', 'function_call', 'content_filter',
        'insufficient_system_resource', 'aborted') else None
    if choice.get('finish_reason') == 'length':
        raise _model_error('模型响应未完成；本轮未覆盖已有方案，可重试', 'format',
                           'incomplete_response', config, started, metrics)
    content = choice['message'].get('content')
    metrics['response_content_chars'] = len(content) if isinstance(content, str) else 0
    if not isinstance(content, str):
        raise _model_error('模型返回内容不完整，未覆盖已有方案', 'format',
                           'invalid_content_shape', config, started, metrics, format_error=True)
    return content, metrics


def _parse_failure(error, stage):
    facts = {'parse_stage': stage, 'parse_error_type': type(error).__name__}
    if isinstance(error, json.JSONDecodeError):
        facts.update(parse_error_pos=error.pos, parse_error_line=error.lineno,
                     parse_error_column=error.colno)
        # JSONDecodeError.msg is parser-owned; never retain doc or arbitrary str(error).
        facts['parse_error_reason'] = error.msg
    return facts


def _parse_model_response(raw, config, started):
    if len(raw) > 2_000_000:
        raise _model_error('模型响应传输超过限制', 'transport', 'response_too_large', config, started)
    try:
        body = json.loads(raw)
    except (TypeError, ValueError, RecursionError) as error:
        raise _model_error('模型返回内容不完整，未覆盖已有方案', 'format',
                           'invalid_response_json', config, started,
                           _parse_failure(error, 'response_json'), format_error=True) from None
    content, metrics = _response_content(body, config, started)
    try:
        content = content.strip()
        if content.startswith('```'):
            content = content.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        data = json.loads(content)
    except (IndexError, TypeError, ValueError, RecursionError) as error:
        raise _model_error('模型返回内容不完整，未覆盖已有方案', 'format',
                           'invalid_content_json', config, started,
                           {**metrics, **_parse_failure(error, 'content_json')}, format_error=True) from None
    if not isinstance(data, dict):
        raise _model_error('模型未返回有效方案对象', 'format', 'invalid_content_shape',
                           config, started, metrics, format_error=True)
    return data, metrics


def call_json(messages, *, vision=False, max_tokens=None, timeout=90, reasoning_effort=None,
              response_schema=None, model_role=None, stream_observability=False,
              max_input_tokens=None, max_input_bytes=None, context_mode=None):
    from .model_sizing import stage_limit
    if max_tokens is None:
        max_tokens = stage_limit('json')
    started = time.monotonic()
    observed = bool(stream_observability and model_role == 'writer' and not vision)
    preflight_required = bool(model_role == 'writer' and not vision
                              and (observed or max_input_tokens is not None))
    config, request = _configured_request(messages, vision, model_role, started, timeout,
        {'max_tokens': max_tokens, 'reasoning_effort': reasoning_effort,
         'response_schema': response_schema, 'stream': observed})
    preflight = {}
    if max_input_bytes is not None:
        try:
            byte_facts = measure_writer_request_size(messages)
        except WriterTokenizerError:
            raise _model_error('上下文大小无法安全度量，未发送请求', 'config',
                'writer_tokenizer_unavailable', config, started,
                {'provider_acknowledged': None, 'request_delivery_status': 'not_sent'}) from None
        byte_facts.update(context_input_byte_limit=max_input_bytes,
                          context_mode=context_mode or 'full')
        if byte_facts['request_bytes'] > max_input_bytes:
            byte_facts.update(provider_acknowledged=None,
                              request_delivery_status='not_sent')
            raise _model_error(
                f'context_input_too_large: 输入为{byte_facts["request_bytes"]} bytes，'
                f'超过{max_input_bytes}上限；请求未发送',
                'config', 'context_input_too_large', config, started, byte_facts) from None
        preflight.update(byte_facts)
    if max_input_tokens is not None:
        preflight.update(context_input_limit=max_input_tokens,
                         context_mode=context_mode or 'full')
    if preflight_required:
        preflight_started = time.monotonic()
        deadline_at = started + timeout
        preflight_size = {}
        try:
            preflight_size = measure_writer_request_size(messages)
            preflight = {**preflight, **preflight_size, **_bounded_writer_preflight(
                messages, model=config['model'], reasoning_effort=reasoning_effort,
                deadline=deadline_at)}
        except _WriterPreflightTimeout:
            facts = {
                **preflight,
                **preflight_size,
                'preflight_status': 'timeout',
                'preflight_input_tokens': None,
                'preflight_ms': max(0, round(
                    (time.monotonic() - preflight_started) * 1000)),
                'total_timeout_ms': max(0, round(timeout * 1000)),
                'timeout_stage': 'preflight', 'timeout_limit': 'total',
                'provider_acknowledged': None,
                'request_delivery_status': 'not_sent',
            }
            raise _model_error('Writer事前度量超过总时限，未发送请求', 'timeout',
                               'request_timeout', config, started, facts,
                               transport_error=True) from None
        except WriterTokenizerError as error:
            facts = {**preflight, **preflight_size,
                     **dict(getattr(error, 'safe_facts', {}) or {})}
            facts.update(preflight_ms=max(0, round(
                (time.monotonic() - preflight_started) * 1000)),
                total_timeout_ms=max(0, round(timeout * 1000)),
                provider_acknowledged=None, request_delivery_status='not_sent')
            raise _model_error('Writer官方tokenizer不可用，未发送请求', 'config',
                'writer_tokenizer_unavailable', config, started,
                facts) from None
        preflight.update(preflight_ms=max(0, round(
            (time.monotonic() - preflight_started) * 1000)),
            total_timeout_ms=max(0, round(timeout * 1000)),
            request_body_bytes=(len(request.data)
                                if isinstance(request.data, bytes) else None))
        if max_input_tokens is not None:
            try:
                enforce_preflight_budget(preflight, limit=max_input_tokens,
                    mode=context_mode or 'full', consumer='writer')
            except ContextAdmissionError as error:
                preflight.update(provider_acknowledged=None,
                                 request_delivery_status='not_sent')
                raise _model_error(str(error), 'config', error.code, config, started,
                                   preflight) from None
        remaining = deadline_at - time.monotonic()
        preflight['transport_budget_before_dispatch_ms'] = max(
            0, round(remaining * 1000))
        if remaining <= 0:
            preflight.update(timeout_stage='preflight', timeout_limit='total',
                             provider_acknowledged=None,
                             request_delivery_status='not_sent')
            raise _model_error('Writer事前度量超过总时限，未发送请求', 'timeout',
                               'request_timeout', config, started, preflight,
                               transport_error=True) from None
    try:
        if observed:
            raw, transport = _read_writer_stream(request, remaining)
        else:
            raw, transport = _read_response(request, timeout), {}
    except urllib.error.HTTPError as error:
        transport_usage = _transport_error_usage(error, config.get('key'))
        if not observed and not getattr(error, '_travel_diagnostic', None):
            authorization = request.get_header('Authorization', '')
            secret = authorization[7:] if authorization.startswith('Bearer ') else None
            error._travel_diagnostic = _capture_http_error(error, secret)
        diagnostics = getattr(error, '_travel_diagnostic', None) or {
            'http_status': error.code,
            'provider_error_body': '',
            'provider_error_body_truncated': False,
            'provider_error_headers': {},
            'provider_error_body_status': 'not_read',
        }
        diagnostics = _redact_configured_secret(diagnostics, config.get('key'))
        diagnostics['provider_error_body'] = _limit_utf8(_redact_configured_secret(
            diagnostics['provider_error_body'], config.get('key'),
            truncated_tail=diagnostics.get('provider_error_body_truncated') is True))
        diagnostics['provider_error_headers'] = {
            key: _limit_utf8(_redact_configured_secret(value, config.get('key'),
                truncated_tail=len(value.encode('utf-8')) >= 512), limit=512)
            for key, value in diagnostics['provider_error_headers'].items()}
        failure = _model_error(f'模型服务暂不可用（HTTP {error.code}），已有方案保留',
            'http', 'http_error', config, started,
            {**_stream_facts(preflight, transport_usage, config.get('key')), **diagnostics},
            http_status=error.code,
            transport_error=error.code in (408, 429) or error.code >= 500)
        error.close()
        raise failure from None
    except OSError as error:
        transport_usage = _transport_error_usage(error, config.get('key'))
        timeout_error = isinstance(error, TimeoutError) or (
            isinstance(error, urllib.error.URLError) and isinstance(error.reason, TimeoutError))
        kind = 'timeout' if timeout_error else 'network'
        safe_code = getattr(error, 'code', None)
        code = ('request_timeout' if timeout_error else safe_code
                if safe_code in MODEL_ERROR_CODES['network'] else 'network_error')
        raise _model_error('模型连接超时或网络不可用，已有方案保留，可重试', kind, code,
                           config, started,
                           _stream_facts(preflight, transport_usage, config.get('key')),
                           transport_error=True) from None
    except WriterTimeoutConfigError:
        raise _model_error('Writer分级超时配置无效，未发送请求', 'config',
                           'invalid_timeout', config, started, preflight) from None
    except WriterStreamError as error:
        if error.code == 'response_too_large':
            # Deterministic local limit: neither JSON repair nor provider fallback.
            raise _model_error('Writer响应传输超过限制，已有方案保留',
                               'transport', error.code, config, started,
                               _stream_facts(preflight,
                                   _transport_error_usage(error, config.get('key')),
                                   config.get('key'))) from None
        code = error.code if error.code in MODEL_ERROR_CODES['format'] else 'invalid_response_json'
        raise _model_error('Writer流式响应不完整，已有方案保留', 'format', code,
                           config, started,
                           _stream_facts(preflight,
                               _transport_error_usage(error, config.get('key')),
                               config.get('key')),
                           format_error=code == 'invalid_response_json') from None
    except (TypeError, ValueError):
        raise _model_error('模型服务配置无效，未发送有效请求', 'config',
                           'invalid_configuration', config, started) from None
    try:
        result, metrics = _parse_model_response(raw, config, started)
    except ModelError as error:
        facts = _stream_facts(preflight, transport, config.get('key'))
        error.usage = {**facts, **error.usage}
        if facts.get('provider_request_id_present'):
            for key in ('provider_request_id', 'provider_request_id_hmac_sha256'):
                error.usage.pop(key, None)
            error.usage.update({k: v for k, v in facts.items() if k.startswith('provider_request_id')})
        raise
    facts = _stream_facts(preflight, transport, config.get('key'))
    metrics = {**facts, **metrics}
    if facts.get('provider_request_id_present'):
        for key in ('provider_request_id', 'provider_request_id_hmac_sha256'):
            metrics.pop(key, None)
        metrics.update({k: v for k, v in facts.items() if k.startswith('provider_request_id')})
    result = normalize_json_response(result, response_schema, metrics)
    return result, metrics


def _combined_usage(current, previous):
    """Keep known receipt subtotals, explicitly marking missing accounting."""
    combined = dict(current)
    for key in ('model_calls', 'input_tokens', 'output_tokens', 'duration_ms', 'reasoning_tokens',
                'request_chars', 'request_bytes', 'preflight_input_tokens'):
        values = (previous.get(key), current.get(key))
        known = [value for value in values if type(value) is int and value >= 0]
        if known:
            combined[key] = sum(known)
        else:
            combined.pop(key, None)
    combined['usage_complete'] = (previous.get('usage_complete') is True
                                  and current.get('usage_complete') is True)
    attempts = []
    for value in (previous.get('transport_attempts'), current.get('transport_attempts')):
        if isinstance(value, list):
            attempts.extend(row for row in value if isinstance(row, dict))
    if attempts:
        combined['transport_attempts'] = attempts
    return combined


def _call_configured(messages, **kwargs):
    """Reuse the configured multimodal provider for a primary billing failure.

    No new account, key or service is discovered. Actual model stays in metrics.
    Image requests and content/format errors never silently switch providers.
    """
    deadline_at = kwargs.pop('_deadline_at', None)
    full_attempt = kwargs.pop('_full_attempt', False)
    requested_timeout = kwargs.get('timeout', 90)
    if deadline_at is not None:
        remaining = max(0.01, deadline_at - time.monotonic())
        reserve = 0 if full_attempt else min(30.0, max(0.02, remaining * 0.2))
        kwargs['timeout'] = min(requested_timeout, max(0.01, remaining - reserve))
    try:
        return call_json(messages, **kwargs)
    except ModelError as error:
        if error.usage.get('error_type') == 'transport':
            raise  # Local resource limits cannot be repaired by another provider.
        # An explicitly selected writer must not silently become the vision model.
        if kwargs.get('model_role') == 'writer' and os.getenv('TRAVEL_WRITER_MODEL'):
            raise
        backup = settings(True)
        fallback_reason = ('primary_http_402' if getattr(error, 'http_status', None) == 402
                           else 'primary_transport' if error.transport_error else None)
        if fallback_reason == 'primary_transport':
            primary = settings(False)
            if (primary.get('base'), primary.get('model'), primary.get('key')) == (
                    backup.get('base'), backup.get('model'), backup.get('key')):
                raise  # A same-provider timeout must not become a duplicate request.
        if (kwargs.get('vision') or fallback_reason is None
                or not backup['key'] or backup['off']):
            raise
        if deadline_at is not None:
            remaining = deadline_at - time.monotonic()
            if remaining <= 0.01:
                raise error
            kwargs['timeout'] = min(requested_timeout, remaining)
        try:
            result, metrics = call_json(messages, **{**kwargs, 'vision': True})
        except ModelError as fallback_error:
            fallback_error.usage = _combined_usage(fallback_error.usage, error.usage)
            raise
        return result, {**_combined_usage(metrics, error.usage),
                        'provider_fallback_reason': fallback_reason}


def call_planner_json(messages, **kwargs):
    """Recover one invalid/empty structured response, without changing model.

    DeepSeek documents occasional empty JSON output. Only transport-format
    failures qualify, not semantic plan rejection, user data or HTTP errors.
    Failed attempts count toward usage; no raw content or reasoning is logged.
    https://api-docs.deepseek.com/guides/json_mode/
    """
    started = time.monotonic()
    outer_deadline = kwargs.pop('_repair_deadline', None)
    dedicated_writer = kwargs.get('model_role') == 'writer' and kwargs.get('stream_observability') is True
    failed = None
    requested_timeout = kwargs.get('timeout', 90)
    if not _valid_timeout(requested_timeout):
        return call_json(messages, **kwargs)  # Produce the same safe config error before budget arithmetic.
    total_budget = requested_timeout + min(30.0, max(0.03, requested_timeout * 0.25))
    if dedicated_writer:
        total_budget = 2 * requested_timeout
    deadline_at = started + total_budget
    if outer_deadline is not None:
        if not isinstance(outer_deadline, (int, float)) or not math.isfinite(outer_deadline):
            raise ValueError('Invalid repair deadline')
        deadline_at = min(deadline_at, outer_deadline)
    for attempt in range(2):
        if not attempt and dedicated_writer and deadline_at <= time.monotonic():
            raise ModelError('writer时间预算已用完，请求未发送',
                             {'error_type': 'timeout', 'error_code': 'request_timeout',
                              'failure_stage': 'writer_admission', 'request_delivery_status': 'not_sent'})
        if dedicated_writer and attempt and deadline_at - time.monotonic() < requested_timeout:
            first_error.usage.update(repair_status='not_started_insufficient_time',
                               failure_stage='repair_admission')
            raise first_error
        outgoing = messages if not attempt else messages + [{'role': 'system', 'content':
            '上一次响应未形成有效JSON。本次请返回单个完整JSON对象，不要输出空内容、Markdown或对象外文字；原任务和约束保持不变。'}]
        try:
            result, metrics = _call_configured(outgoing, _deadline_at=deadline_at,
                                               _full_attempt=dedicated_writer, **kwargs)
            if failed:
                metrics = {**_combined_usage(metrics, failed), 'structured_output_retries': 1,
                           'failed_response_chars': failed.get('response_content_chars'),
                           'structured_output_failures': [failed]}
            metrics['elapsed_ms'] = max(0, round((time.monotonic() - started) * 1000))
            return result, metrics
        except ModelError as error:
            if failed:
                error.usage = {**_combined_usage(error.usage, failed), 'structured_output_retries': 1,
                               'structured_output_failures': [failed]}
            error.usage['elapsed_ms'] = max(0, round((time.monotonic() - started) * 1000))
            if attempt or not error.format_error:
                raise
            failed = dict(error.usage)
            first_error = error
            error.usage['structured_output_failures'] = [failed]
            if not dedicated_writer and deadline_at - time.monotonic() <= 0.01:
                raise
