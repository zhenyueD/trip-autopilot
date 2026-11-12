"""Streaming writer transport with bounded, privacy-safe phase evidence."""
from __future__ import annotations

import json
import math
import os
import queue
import socket
import threading
import time
import urllib.error
import urllib.request

from .writer_transport_http import (
    _ObservedHTTPSConnection, _open_response, _prepare_http_error,
    _response_socket,
)


_TRANSPORTS = threading.BoundedSemaphore(8)
_TOKEN_COUNTERS = threading.BoundedSemaphore(4)
# 16k output tokens may each carry a separate SSE envelope. Keep independent
# finite limits for event overhead and decoded reasoning + content.
_MAX_STREAM_EVENTS = 32_768
_MAX_STREAM_BYTES = 32 * 1024 * 1024
_MAX_EVENT_BYTES = 2 * 1024 * 1024
_MAX_OUTPUT_BYTES = 1024 * 1024
_HTTP_ERROR_BODY_TIMEOUT = 0.25
_REQUEST_ID_HEADERS = ('x-request-id', 'request-id', 'openai-request-id',
                       'x-openai-request-id', 'x-deepseek-request-id')
_PHASE_METRICS = {
    'connect': 'connect_ms',
    'tls': 'tls_ms',
    'write': 'request_write_ms',
    'inter_token': 'max_inter_token_gap_ms',
}


class WriterTransportTimeout(TimeoutError):
    """A safe timeout whose usage contains no prompt or provider content."""

    def __init__(self, stage, usage):
        super().__init__('Writer transport deadline exceeded')
        self.stage = stage
        self.usage = usage


class WriterStreamError(ValueError):
    """A bounded stream-format failure with safe transport evidence."""

    def __init__(self, code, usage):
        super().__init__('Writer stream response was incomplete or invalid')
        self.code = code
        self.usage = usage


class WriterTimeoutConfigError(ValueError):
    """Raised before transport when a tiered timeout is invalid."""


def _seconds(name, default, total):
    raw = os.getenv(name)
    try:
        value = default if raw in (None, '') else float(raw)
    except (TypeError, ValueError):
        raise WriterTimeoutConfigError('invalid writer timeout') from None
    if not math.isfinite(value) or value <= 0:
        raise WriterTimeoutConfigError('invalid writer timeout')
    return min(value, total)


def timeout_config(total):
    if type(total) not in (int, float) or not math.isfinite(total) or total <= 0:
        raise WriterTimeoutConfigError('invalid writer timeout')
    connect = _seconds('TRAVEL_WRITER_CONNECT_TIMEOUT_SECONDS', 15.0, total)
    first = _seconds('TRAVEL_WRITER_FIRST_TOKEN_TIMEOUT_SECONDS', 90.0, total)
    inter = _seconds('TRAVEL_WRITER_INTER_TOKEN_TIMEOUT_SECONDS', 30.0, total)
    return {'connect': connect, 'tls': connect, 'write': connect,
            'first_token': first, 'inter_token': inter, 'total': float(total)}


def _milliseconds(value):
    return max(0, round(value * 1000))


def _safe_request_id(headers, configured_secret=None):
    if headers is None:
        return None
    for key in _REQUEST_ID_HEADERS:
        value = headers.get(key)
        if (isinstance(value, str) and 0 < len(value) <= 256
                and all(char.isascii() and (char.isalnum() or char in '._:/-')
                        for char in value)
                and 'sk-' not in value.lower() and '://' not in value):
            if (isinstance(configured_secret, str) and configured_secret
                    and configured_secret in value):
                return None
            return value
    return None


class _TransportTrace:
    def __init__(self, total_timeout, model, *, request_body_bytes=None,
                 configured_secret=None, clock=time.monotonic):
        self.clock = clock
        self.config = timeout_config(total_timeout)
        self.model = model
        self.request_body_bytes = (request_body_bytes
                                   if type(request_body_bytes) is int
                                   and request_body_bytes >= 0 else None)
        self._configured_secret = configured_secret
        self.started = clock()
        self.total_deadline = self.started + self.config['total']
        self.local_accounting_reserve = min(
            0.25, max(0.005, self.config['total'] * 0.05),
            self.config['total'] * 0.2)
        self.provider_deadline = self.total_deadline - self.local_accounting_reserve
        self.condition = threading.Condition()
        self.phase = 'connect'
        self.phase_started = self.started
        self.phase_deadline = min(self.provider_deadline,
                                  self.started + self.config['connect'])
        self.socket = None
        self.finished = False
        self.result = None
        self.error = None
        self.timings = {name: None for name in (
            'connect_ms', 'tls_ms', 'request_write_ms', 'response_headers_ms',
            'ttft_ms', 'stream_ms', 'max_inter_token_gap_ms')}
        self.write_started_at = None
        self.request_written_at = None
        self.headers_at = None
        self.first_token_at = None
        self.last_token_at = None
        self.done_at = None
        self.provider_terminal_at = None
        self.sse_events = 0
        self.sse_payload_events = 0
        self.stream_wire_bytes = 0
        self.stream_output_bytes = 0
        self.stream_limit = None
        self.content_delta_events = 0
        self.reasoning_delta_events = 0
        self.output_parts = []
        self.output_estimated_tokens = None
        self.output_tokenized_chars = None
        self.output_token_count_status = 'pending'
        self.local_token_accounting_seconds = 0.0
        self._output_count_lock = threading.Lock()
        self._output_count_event = threading.Event()
        self._output_count_result = None
        self._terminal_lock = threading.Lock()
        self.terminal_stage = None
        self.terminal_limit = None
        self.provider_request_id = None
        self.provider_acknowledged = None
        self.request_delivery_status = 'not_sent'
        self.provider_input_tokens = None
        self.provider_output_tokens = None
        self.provider_reasoning_tokens = None
        self.http_status = None
        self.http_error_body_status = None
        self.pending_http_error = None
        self.pending_stream_error = None
        self.pending_stream_stage = None
        self.success_ready_at = None

    def _notify(self):
        with self.condition:
            self.condition.notify_all()

    def begin(self, phase):
        now = self.clock()
        with self.condition:
            self.phase = phase
            self.phase_started = now
            phase_timeout = self.config.get(phase)
            self.phase_deadline = (self.total_deadline if phase_timeout is None else
                                   min(self.provider_deadline, now + phase_timeout))
            self.condition.notify_all()
        return now

    def complete(self, phase, started, metric=None):
        duration = max(0.0, self.clock() - started)
        if metric:
            self.timings[metric] = _milliseconds(duration)
        return duration

    def remaining(self, phase=None):
        now = self.clock()
        phase_deadline = self.phase_deadline
        if phase is not None and phase != self.phase:
            phase_deadline = min(self.total_deadline, now + self.config[phase])
        remaining = min(self.total_deadline, phase_deadline) - now
        if remaining <= 0:
            raise self.timeout_error()
        return remaining

    def track_socket(self, value):
        with self.condition:
            self.socket = value
            self.condition.notify_all()

    def request_send_started(self):
        if self.write_started_at is None:
            self.write_started_at = self.begin('write')
        if self.request_delivery_status == 'not_sent':
            self.request_delivery_status = 'sent_unacknowledged'

    def request_complete(self):
        self.request_written_at = self.clock()
        if self.write_started_at is not None:
            self.timings['request_write_ms'] = _milliseconds(
                self.request_written_at - self.write_started_at)
        self.begin('first_token')

    def headers_received(self, headers):
        now = self.clock()
        self.headers_at = now
        if self.request_written_at is not None:
            self.timings['response_headers_ms'] = _milliseconds(
                now - self.request_written_at)
        self.provider_acknowledged = True
        self.request_delivery_status = 'acknowledged'
        self.provider_request_id = _safe_request_id(headers, self._configured_secret)
        self._notify()

    def token_delta(self, value, kind):
        if not isinstance(value, str) or not value:
            return
        size = len(value.encode('utf-8'))
        self.check_stream_limit('generated_text', self.stream_output_bytes + size, _MAX_OUTPUT_BYTES)
        self.stream_output_bytes += size
        now = self.clock()
        with self.condition:
            if self.first_token_at is None:
                self.first_token_at = now
                base = (self.request_written_at
                        if self.request_written_at is not None else self.started)
                self.timings['ttft_ms'] = _milliseconds(now - base)
            elif self.last_token_at is not None:
                gap = _milliseconds(now - self.last_token_at)
                prior = self.timings['max_inter_token_gap_ms']
                self.timings['max_inter_token_gap_ms'] = (
                    gap if prior is None else max(prior, gap))
            self.output_parts.append(value)
            if kind == 'content':
                self.content_delta_events += 1
            else:
                self.reasoning_delta_events += 1
            self.last_token_at = now
            self.phase = 'inter_token'
            self.phase_started = now
            self.phase_deadline = min(
                self.provider_deadline, now + self.config['inter_token'])
            self.condition.notify_all()

    def record_response_id(self, value):
        # Headers take precedence; retain the first valid body ID immediately
        # so a later timeout or malformed event cannot discard it.
        with self.condition:
            if self.provider_request_id is None:
                self.provider_request_id = _safe_request_id(
                    {'x-request-id': value}, self._configured_secret)

    def check_stream_limit(self, kind, observed, limit):
        if observed > limit:
            self.stream_limit = kind
            self.stream_limit_observed = observed
            self.stream_limit_max = limit
            raise WriterStreamError('response_too_large', self.snapshot())

    def stream_event(self):
        self.sse_events += 1

    def record_provider_usage(self, usage):
        if not isinstance(usage, dict):
            return
        for source, attribute in (
                ('prompt_tokens', 'provider_input_tokens'),
                ('completion_tokens', 'provider_output_tokens')):
            value = usage.get(source)
            if type(value) is int and value >= 0:
                setattr(self, attribute, value)
        details = usage.get('completion_tokens_details')
        reasoning = details.get('reasoning_tokens') if isinstance(details, dict) else None
        if type(reasoning) is int and reasoning >= 0:
            self.provider_reasoning_tokens = reasoning

    def stream_done(self):
        now = self.clock()
        with self.condition:
            self.done_at = now
            self.provider_terminal_at = now
            if self.first_token_at is not None:
                self.timings['stream_ms'] = _milliseconds(
                    self.done_at - self.first_token_at)
            self.phase = 'local_accounting'
            self.phase_started = now
            self.phase_deadline = self.total_deadline
            self.condition.notify_all()

    def provider_terminal(self):
        """Freeze provider-generation timing before local evidence work starts."""
        now = self.clock()
        with self.condition:
            if self.provider_terminal_at is None:
                self.provider_terminal_at = now
            if self.first_token_at is not None and self.timings['stream_ms'] is None:
                self.timings['stream_ms'] = _milliseconds(
                    self.provider_terminal_at - self.first_token_at)
            self.condition.notify_all()
        return now

    def http_error_received(self, error):
        """Latch a known HTTP response before its optional body is inspected."""
        now = self.clock()
        headers = getattr(error, 'headers', None)
        with self.condition:
            if self.provider_acknowledged is not True:
                self.headers_at = now
                if self.request_written_at is not None:
                    self.timings['response_headers_ms'] = _milliseconds(
                        now - self.request_written_at)
                self.provider_acknowledged = True
                self.request_delivery_status = 'acknowledged'
                self.provider_request_id = _safe_request_id(
                    headers, self._configured_secret)
            if self.provider_terminal_at is None:
                self.provider_terminal_at = now
            status = getattr(error, 'code', None)
            self.http_status = (status if type(status) is int
                                and 100 <= status <= 599 else None)
            self.pending_http_error = error
            self.phase = 'http_error_body'
            self.phase_started = now
            self.phase_deadline = min(
                self.total_deadline, now + _HTTP_ERROR_BODY_TIMEOUT)
            self.condition.notify_all()
        # No SSE body was accepted.  This is a known zero-length prefix, not
        # unknown output accounting; the provider's bill remains unknown.
        self.finalize_output_count(max_wait=0)

    def http_error_body_finished(self, status):
        with self.condition:
            if status in ('complete', 'timeout', 'error'):
                self.http_error_body_status = status
            self.condition.notify_all()

    def detached_http_error(self):
        """Return a body-independent copy when the HTTP status is already known."""
        with self.condition:
            source = self.pending_http_error
        if not isinstance(source, urllib.error.HTTPError):
            return None
        self.http_error_body_status = 'timeout'
        error = urllib.error.HTTPError(
            getattr(source, 'url', ''), source.code, source.msg,
            source.headers, None)
        error._travel_diagnostic = {
            'http_status': source.code,
            'provider_error_body': '',
            'provider_error_body_truncated': False,
            'provider_error_headers': {},
            'provider_error_body_status': 'timeout',
        }
        usage = self.snapshot()
        usage['transport_attempts'] = [dict(usage)]
        error.usage = usage
        error._writer_transport_usage = dict(usage)
        return error

    def lock_stream_failure(self, error, stage):
        """Publish the primary failure before optional local token accounting."""
        with self.condition:
            self.pending_stream_error = error
            self.pending_stream_stage = stage
            self.provider_terminal()
            self.condition.notify_all()

    def detached_stream_error(self):
        """The waiter may reach the deadline while the worker counts tokens."""
        with self.condition:
            source = self.pending_stream_error
            stage = self.pending_stream_stage
        if not isinstance(source, WriterStreamError):
            return None
        with self._output_count_lock:
            if self.output_token_count_status in ('pending', 'running'):
                self.output_token_count_status = 'timeout'
        usage = self.snapshot()
        usage['failure_stage'] = stage
        usage['transport_attempts'] = [dict(usage)]
        return WriterStreamError(source.code, usage)

    def timeout_stage(self):
        return self.phase

    def timeout_limit(self, stage=None):
        if self.clock() >= self.provider_deadline:
            return 'total'
        stage = stage or self.phase
        return ('connect' if stage in ('connect', 'tls', 'write') else
                stage if stage in ('first_token', 'inter_token') else 'total')

    def cancel(self):
        sock = self.socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def finalize_output_count(self, max_wait=None):
        """Count the full received prefix once, bounded by the call deadline.

        BPE counts are not additive across SSE chunk boundaries. A latest full
        prefix count is therefore both cheaper and more accurate than summing
        per-delta token counts.
        """
        with self._output_count_lock:
            if self.output_token_count_status in ('complete', 'error', 'timeout', 'capacity'):
                return
            text = ''.join(self.output_parts)
            if not text:
                self.output_estimated_tokens = 0
                self.output_tokenized_chars = 0
                self.output_token_count_status = 'complete'
                self._output_count_event.set()
                return
            if self.output_token_count_status == 'pending':
                if not _TOKEN_COUNTERS.acquire(blocking=False):
                    self.output_token_count_status = 'capacity'
                    self._output_count_event.set()
                    return
                self.output_token_count_status = 'running'
                def count():
                    try:
                        from .writer_tokenizer import count_writer_text
                        self._output_count_result = (
                            count_writer_text(text, model=self.model), len(text))
                    except Exception:
                        self._output_count_result = None
                    finally:
                        _TOKEN_COUNTERS.release()
                        self._output_count_event.set()
                try:
                    threading.Thread(target=count, name='writer-output-token-count',
                                     daemon=True).start()
                except RuntimeError:
                    _TOKEN_COUNTERS.release()
                    self.output_token_count_status = 'error'
                    self._output_count_event.set()
                    return
        accounting_started = self.clock()
        remaining = max(0.0, self.total_deadline - accounting_started)
        cap = self.local_accounting_reserve if max_wait is None else max(0.0, max_wait)
        remaining = min(remaining, cap)
        finished = self._output_count_event.wait(timeout=remaining)
        accounting_done = self.clock()
        with self._output_count_lock:
            if self.output_token_count_status != 'running':
                return
            self.local_token_accounting_seconds += max(
                0.0, accounting_done - accounting_started)
            if not finished:
                self.output_token_count_status = 'timeout'
                return
            result = self._output_count_result
            if (isinstance(result, tuple) and len(result) == 2
                    and type(result[0]) is int and result[0] >= 0
                    and type(result[1]) is int and result[1] >= 0):
                self.output_estimated_tokens, self.output_tokenized_chars = result
                self.output_token_count_status = 'complete'
            else:
                self.output_token_count_status = 'error'

    def snapshot(self, timeout_stage=None, timeout_limit=None):
        now = self.clock()
        timings = dict(self.timings)
        metric = _PHASE_METRICS.get(self.phase)
        if self.done_at is None and metric:
            phase_end = self.provider_terminal_at or now
            in_progress = _milliseconds(phase_end - self.phase_started)
            previous = timings.get(metric)
            # A prior short delta must not hide the final long stalled gap.
            timings[metric] = (in_progress if previous is None else
                               max(previous, in_progress))
        tokens = self.output_estimated_tokens
        # Local tokenizer/accounting latency is not provider generation time.
        stream_end = self.provider_terminal_at or self.done_at or now
        generation_seconds = (stream_end - self.first_token_at
                              if self.first_token_at is not None else None)
        rate = (round(tokens / generation_seconds, 3)
                if type(tokens) is int and tokens > 0 and generation_seconds
                and generation_seconds > 0 else None)
        result = {**timings,
            'request_body_bytes': self.request_body_bytes,
            # Keep TTFT null until a token truly arrives. On failure the
            # bounded wait is useful, but it is not a completed TTFT.
            'first_token_wait_ms': (_milliseconds(
                                        (self.provider_terminal_at or now)
                                        - self.phase_started)
                                    if self.done_at is None
                                    and self.phase == 'first_token'
                                    and self.first_token_at is None else None),
            'stream_output_chars': sum(len(value) for value in self.output_parts),
            'stream_output_estimated_tokens': tokens,
            'stream_output_tokenized_chars': self.output_tokenized_chars,
            'stream_output_token_count_kind': 'official_full_text',
            'stream_output_token_count_status': self.output_token_count_status,
            'local_token_accounting_status': self.output_token_count_status,
            'local_token_accounting_ms': _milliseconds(
                self.local_token_accounting_seconds),
            'generation_tokens_per_second': rate,
            'sse_events': self.sse_events,
            'sse_payload_events': self.sse_payload_events,
            'stream_wire_bytes': self.stream_wire_bytes,
            'stream_output_bytes': self.stream_output_bytes,
            'stream_limit': self.stream_limit,
            'stream_limit_observed': getattr(self, 'stream_limit_observed', None),
            'stream_limit_max': getattr(self, 'stream_limit_max', None),
            'content_delta_events': self.content_delta_events,
            'reasoning_delta_events': self.reasoning_delta_events,
            'connect_timeout_ms': _milliseconds(self.config['connect']),
            'first_token_timeout_ms': _milliseconds(self.config['first_token']),
            'inter_token_timeout_ms': _milliseconds(self.config['inter_token']),
            'transport_timeout_ms': _milliseconds(
                self.provider_deadline - self.started),
            'local_accounting_reserve_ms': _milliseconds(
                self.local_accounting_reserve),
            'total_timeout_ms': _milliseconds(self.config['total']),
            'timeout_stage': timeout_stage,
            'timeout_limit': timeout_limit,
            'provider_request_id': self.provider_request_id,
            'provider_acknowledged': self.provider_acknowledged,
            'request_delivery_status': self.request_delivery_status,
            'elapsed_ms': _milliseconds(now - self.started)}
        if type(self.http_status) is int:
            result['http_status'] = self.http_status
        if self.http_error_body_status in ('complete', 'timeout', 'error'):
            result['provider_error_body_status'] = self.http_error_body_status
        if type(self.provider_input_tokens) is int:
            result['input_tokens'] = self.provider_input_tokens
        if type(self.provider_output_tokens) is int:
            result['output_tokens'] = self.provider_output_tokens
        if type(self.provider_reasoning_tokens) is int:
            result['reasoning_tokens'] = self.provider_reasoning_tokens
        result['usage_complete'] = all(type(value) is int for value in (
            self.provider_input_tokens, self.provider_output_tokens))
        return result

    def timeout_error(self, stage=None, limit=None):
        stage, limit = self.latch_timeout(stage, limit)
        usage = self.snapshot(stage, limit)
        usage['transport_attempts'] = [dict(usage)]
        return WriterTransportTimeout(stage, usage)

    def latch_timeout(self, stage=None, limit=None):
        with self._terminal_lock:
            if self.terminal_stage is None:
                selected_stage = stage or self.timeout_stage()
                self.terminal_stage = selected_stage
                self.terminal_limit = limit or self.timeout_limit(selected_stage)
                if self.provider_terminal_at is None:
                    self.provider_terminal_at = self.clock()
                if (self.first_token_at is not None
                        and self.timings['stream_ms'] is None):
                    self.timings['stream_ms'] = _milliseconds(
                        self.provider_terminal_at - self.first_token_at)
            return self.terminal_stage, self.terminal_limit

    def finalized_timeout_error(self, stage=None, limit=None):
        stage, limit = self.latch_timeout(stage, limit)
        phase_usage = self.snapshot(stage, limit)
        self.cancel()
        self.finalize_output_count(max_wait=self.local_accounting_reserve)
        usage = self.snapshot(stage, limit)
        for key in (*self.timings, 'first_token_wait_ms'):
            if usage.get(key) is None and phase_usage.get(key) is not None:
                usage[key] = phase_usage[key]
        usage['transport_attempts'] = [dict(usage)]
        return WriterTransportTimeout(stage, usage)


def _sse_payloads(response, trace):
    event_bytes = 0
    event = []
    while True:
        sock = _response_socket(response)
        if sock is not None:
            trace.track_socket(sock)
            sock.settimeout(trace.remaining())
        try:
            line = response.readline(min(_MAX_EVENT_BYTES - event_bytes,
                                         _MAX_STREAM_BYTES - trace.stream_wire_bytes) + 1)
        except TimeoutError:
            raise trace.timeout_error() from None
        trace.stream_wire_bytes += len(line)
        event_bytes += len(line)
        trace.check_stream_limit('wire_bytes', trace.stream_wire_bytes, _MAX_STREAM_BYTES)
        trace.check_stream_limit('event_bytes', event_bytes, _MAX_EVENT_BYTES)
        if not line:
            if event:
                trace.sse_payload_events += 1
                trace.check_stream_limit('event_count', trace.sse_payload_events, _MAX_STREAM_EVENTS)
                yield b'\n'.join(event)
            return
        stripped = line.rstrip(b'\r\n')
        if not stripped:
            if event:
                trace.sse_payload_events += 1
                trace.check_stream_limit('event_count', trace.sse_payload_events, _MAX_STREAM_EVENTS)
                yield b'\n'.join(event)
                event = []
            event_bytes = 0
            continue
        if stripped.startswith(b'data:'):
            event.append(stripped[5:].lstrip(b' '))


def _consume_sse(response, trace):
    content, usage, model, response_id = [], None, None, None
    finish_reason = None
    saw_done = False
    for payload in _sse_payloads(response, trace):
        if payload == b'[DONE]':
            saw_done = True
            trace.stream_done()
            break
        try:
            body = json.loads(payload)
        except (TypeError, ValueError, RecursionError):
            raise WriterStreamError('invalid_response_json', trace.snapshot()) from None
        if not isinstance(body, dict):
            continue
        trace.stream_event()
        trace.record_response_id(body.get('id'))
        response_id = body.get('id') if isinstance(body.get('id'), str) else response_id
        model = body.get('model') if isinstance(body.get('model'), str) else model
        if isinstance(body.get('usage'), dict):
            usage = body['usage']
            trace.record_provider_usage(usage)
        choices = body.get('choices')
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            continue
        choice = choices[0]
        delta = choice.get('delta')
        if isinstance(delta, dict):
            value = delta.get('reasoning_content')
            trace.token_delta(value, 'reasoning')
            value = delta.get('content')
            if isinstance(value, str) and value:
                trace.token_delta(value, 'content')
                content.append(value)
        if isinstance(choice.get('finish_reason'), str):
            finish_reason = choice['finish_reason']
    if not saw_done:
        raise WriterStreamError('incomplete_response', trace.snapshot())
    trace.finalize_output_count()
    trace.begin('local_finalize')
    body = {'id': response_id, 'model': model,
            'choices': [{'finish_reason': finish_reason,
                         'message': {'content': ''.join(content)}}]}
    if usage is not None:
        body['usage'] = usage
    raw = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
    return raw


def _stream_unbudgeted(request, timeout):
    try:
        payload = json.loads(request.data)
        model = payload.get('model') if isinstance(payload, dict) else None
    except (TypeError, ValueError, AttributeError):
        model = None
    body = getattr(request, 'data', None)
    authorization = request.get_header('Authorization', '')
    configured_secret = (authorization[7:]
                         if authorization.startswith('Bearer ') else None)
    trace = _TransportTrace(timeout, model,
                            request_body_bytes=len(body) if isinstance(body, bytes) else None,
                            configured_secret=configured_secret)
    output = queue.Queue(maxsize=1)
    if not _TRANSPORTS.acquire(blocking=False):
        trace.finalize_output_count(max_wait=0)
        error = OSError('Writer transport capacity unavailable')
        error.code = 'transport_capacity'
        error.usage = trace.snapshot()
        raise error

    def worker():
        response = None
        try:
            response = _open_response(request, trace)
            raw = _consume_sse(response, trace)
            # Readiness, deadline validation and publication share the same
            # condition as the consumer.  A late snapshot can never be queued
            # as success, while a timely result remains valid if the consumer
            # is scheduled after the deadline.
            with trace.condition:
                metrics = trace.snapshot()
                completed_at = trace.clock()
                if completed_at >= trace.total_deadline:
                    raise trace.timeout_error('local_finalize', 'total')
                trace.success_ready_at = completed_at
                metrics['elapsed_ms'] = _milliseconds(
                    completed_at - trace.started)
                metrics['transport_attempts'] = [dict(metrics)]
                output.put_nowait((True, (raw, metrics)))
                trace.condition.notify_all()
        except Exception as error:
            failure_stage = trace.phase
            if isinstance(error, WriterStreamError):
                trace.lock_stream_failure(error, failure_stage)
            if isinstance(error, urllib.error.HTTPError):
                # The status and response headers are authoritative as soon as
                # urllib raises.  Optional body diagnostics must never race a
                # known 4xx/5xx into a false first-token timeout.
                trace.http_error_received(error)
                try:
                    _prepare_http_error(error, request, trace)
                    diagnostic = getattr(error, '_travel_diagnostic', {})
                    trace.http_error_body_finished(
                        diagnostic.get('provider_error_body_status', 'complete'))
                except WriterTransportTimeout:
                    error._travel_diagnostic = {
                        'http_status': error.code,
                        'provider_error_body': '',
                        'provider_error_body_truncated': False,
                        'provider_error_headers': {},
                        'provider_error_body_status': 'timeout',
                    }
                    trace.http_error_body_finished('timeout')
                except OSError:
                    # Status remains known; a diagnostic-body read failure must
                    # not leak an exception or extend the writer deadline.
                    error._travel_diagnostic = {
                        'http_status': error.code,
                        'provider_error_body': '',
                        'provider_error_body_truncated': False,
                        'provider_error_headers': {},
                        'provider_error_body_status': 'error',
                    }
                    trace.http_error_body_finished('error')
                trace.provider_terminal()
            else:
                trace.provider_terminal()
            provider_phase_usage = trace.snapshot()
            if isinstance(error, urllib.error.HTTPError):
                # Once a valid HTTP status is known it outranks all later,
                # optional diagnostic-body or local-accounting deadlines.
                pass
            elif isinstance(error, WriterTransportTimeout):
                error = trace.finalized_timeout_error(
                    error.stage, error.usage.get('timeout_limit'))
            else:
                trace.begin('local_accounting')
                trace.finalize_output_count()
                if (trace.clock() >= trace.total_deadline
                        and not isinstance(error, WriterStreamError)):
                    error = trace.timeout_error('local_accounting', 'total')
            reason = getattr(error, 'reason', None)
            inherited = getattr(reason, 'usage', None)
            if isinstance(inherited, dict):
                error.usage = dict(inherited)
            elif isinstance(error, WriterTransportTimeout):
                pass
            else:
                previous = getattr(error, 'usage', {})
                timeout_stage = (previous.get('timeout_stage')
                                 if isinstance(previous, dict) else None)
                timeout_limit = (previous.get('timeout_limit')
                                 if isinstance(previous, dict) else None)
                error.usage = trace.snapshot(timeout_stage, timeout_limit)
                # Preserve the provider-phase duration frozen before local
                # token accounting changes the active phase.
                for key in (*trace.timings, 'first_token_wait_ms'):
                    value = provider_phase_usage.get(key)
                    if value is not None:
                        error.usage[key] = value
                error.usage['failure_stage'] = failure_stage
            if isinstance(error.usage, dict) and not error.usage.get('transport_attempts'):
                error.usage['transport_attempts'] = [dict(error.usage)]
            if isinstance(error, urllib.error.HTTPError):
                error._writer_transport_usage = dict(error.usage)
            output.put((False, error))
        finally:
            try:
                if response is not None:
                    response.close()
            except Exception:
                pass
            finally:
                _TRANSPORTS.release()
                with trace.condition:
                    trace.finished = True
                    trace.condition.notify_all()

    try:
        from .phase1_budget_context import context_target
        threading.Thread(target=context_target(worker), name='writer-stream-transport',
                         daemon=True).start()
    except RuntimeError:
        _TRANSPORTS.release()
        trace.finalize_output_count(max_wait=0)
        error = OSError('Writer transport could not start')
        error.code = 'transport_start_failed'
        error.usage = trace.snapshot()
        raise error from None

    while True:
        # The queue is rechecked while holding the same condition used by the
        # worker notification. This closes the put+notify-before-wait race that
        # could otherwise sleep until the phase deadline after a result existed.
        with trace.condition:
            try:
                success, value = output.get_nowait()
                if success:
                    if (trace.success_ready_at is None
                            or trace.success_ready_at >= trace.total_deadline):
                        raise trace.finalized_timeout_error(
                            'local_finalize', 'total')
                    return value
                raise value
            except queue.Empty:
                pass
            now = trace.clock()
            deadline = min(trace.total_deadline, trace.phase_deadline)
            wait = max(0.0, deadline - now)
            if wait <= 0:
                stream_error = trace.detached_stream_error()
                if stream_error is not None:
                    trace.cancel()
                    raise stream_error
                http_error = trace.detached_http_error()
                if http_error is not None:
                    trace.cancel()
                    raise http_error
                stage = trace.timeout_stage()
                limit = trace.timeout_limit(stage)
                error = trace.finalized_timeout_error(stage, limit)
                raise error
            trace.condition.wait(timeout=wait)


def read_stream_response(request, timeout):
    """Execute one physical writer SSE request under the shared budget."""
    from .phase1_budget_context import current_budget, physical_attempt, transport_usage
    prepare = getattr(current_budget(), 'prepare_http_request', None)
    if callable(prepare):
        prepare(request)
    with physical_attempt('model_http', timeout) as attempt:
        raw, metrics = _stream_unbudgeted(request, attempt.timeout)
        if attempt.lease is not None:
            attempt.usage = transport_usage(raw)
        return raw, metrics
