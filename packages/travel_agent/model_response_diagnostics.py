"""Safe, frozen failure evidence for the nonstream HTTP worker."""
import json
import re
import threading


_COUNTS = ('input_tokens', 'output_tokens', 'reasoning_tokens', 'total_tokens',
           'cached_input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')

_SECRET_FIELD = re.compile(
    r'(?:api[-_]?key|private[-_]?key|authorization|credential|password|secret|cookie|'
    r'token$)', re.I)
_TEXT_SECRET = re.compile(
    r'''(?ix)(\b(?:api[-_]?key|private[-_]?key|authorization|credential|password|secret|'''
    r'''[a-z_-]*token|cookie)\b["']?\s*[:=]\s*)'''
    r'''(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s;,<>]+)''')


def redact_recorded_body(raw, secrets=()):
    """Retain complete HTTP evidence, applying the existing credential redactors.

    JSON is only reserialized when redaction is required. Token count fields are
    evidence, not credentials. This never changes the HTTP response seen by SDKs.
    """
    from .model import _redact_configured_secret, _redact_error_value

    text = raw.decode('utf-8', errors='replace')
    for secret in secrets:
        text = _redact_configured_secret(text, secret)

    def redact(value):
        if isinstance(value, dict):
            return {redact(key): '<REDACTED>' if _SECRET_FIELD.search(str(key)) else redact(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        for secret in secrets:
            value = _redact_configured_secret(value, secret)
        value = _redact_error_value(value)
        return _TEXT_SECRET.sub(r'\1<REDACTED>', value) if isinstance(value, str) else value

    try:
        parsed = json.loads(text)
        safe = redact(parsed)
        return json.dumps(safe, ensure_ascii=False) if safe != parsed else text
    except (TypeError, ValueError):
        return _TEXT_SECRET.sub(r'\1<REDACTED>', _redact_error_value(text))


class ResponseDiagnostics:
    def __init__(self, request, request_id_headers, sanitize_id):
        get_header = request.get_header if request is not None else lambda name, default=None: default
        authorization = get_header('Authorization', '')
        bearer = authorization[7:] if authorization.startswith('Bearer ') else None
        self.secrets = tuple(value for value in (bearer,
            get_header('X-goog-api-key'), get_header('X-api-key'))
            if isinstance(value, str) and value)
        self.secret = next(iter(self.secrets), None)
        self._headers, self._sanitize_id = request_id_headers, sanitize_id
        self._lock = threading.Lock()
        self._published, self._failure, self._complete = False, None, None
        self._facts = {'timeout_stage': 'preflight', 'request_delivery_status': 'not_sent',
                       'provider_acknowledged': None, 'provider_request_id_present': False}

    def stage(self, stage, response=None):
        ids = {}
        if response is not None:
            headers = getattr(response, 'headers', None)
            try:
                normalized = {str(k).lower(): v for k, v in headers.items()} if headers else {}
                for key in self._headers:
                    if normalized.get(key):
                        value = normalized[key]
                        secret = next((item for item in self.secrets
                                       if isinstance(value, str) and item in value), self.secret)
                        ids = self._sanitize_id(value, secret)
                        break
            except Exception:
                pass  # Optional correlation metadata must never replace the transport error.
        with self._lock:
            if self._published:
                return
            self._facts.update(timeout_stage=stage,
                request_delivery_status=('response_received' if stage == 'response-read'
                                         else 'unknown' if stage == 'transport' else 'not_sent'),
                provider_acknowledged=True if stage == 'response-read' else None, **ids)

    def _merge_usage(self, usage):
        if not isinstance(usage, dict):
            return
        self._facts.update({key: usage[key] for key in _COUNTS
                            if type(usage.get(key)) is int and usage[key] >= 0})
        if usage.get('usage_status') == 'unknown':
            self._complete = False
        elif type(usage.get('usage_complete')) is bool:
            self._complete = usage['usage_complete']

    def usage(self, usage):
        with self._lock:
            if not self._published:
                self._merge_usage(usage)

    def inherit_response_id(self, facts):
        """Copy only our inner transport's already-sanitized header evidence."""
        with self._lock:
            if not self._published and isinstance(facts, dict):
                self._facts.update({key: facts[key] for key in (
                    'provider_request_id', 'provider_request_id_present',
                    'provider_request_id_hmac_sha256') if key in facts})

    def fail(self, error, diagnostic=None):
        with self._lock:
            if self._published:
                return
            if self._failure is None:
                self._failure = error
                self._merge_usage(getattr(getattr(error, 'reason', None), 'usage', None))
                self._merge_usage(getattr(error, 'usage', None))
            if diagnostic is not None:
                error._travel_diagnostic = diagnostic

    def _snapshot(self):
        complete = (self._complete is not False and
                    all(key in self._facts for key in ('input_tokens', 'output_tokens')))
        return {**self._facts, 'usage_complete': complete,
                'usage_status': 'known' if complete else 'unknown'}

    def finish(self):
        with self._lock:
            self._published = True
            return self._snapshot()

    def publish(self, fallback):
        with self._lock:
            error = self._failure if self._failure is not None else fallback
            if not self._published:
                self._merge_usage(getattr(error, 'usage', None))
                error.usage = self._snapshot()
                error._travel_nonstream_facts = True
                self._published = True
            return error
