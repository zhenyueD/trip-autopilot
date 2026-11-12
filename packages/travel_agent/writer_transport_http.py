"""Observed stdlib HTTP connections used by the writer SSE transport."""
from __future__ import annotations

import functools
import http.client
import ssl
import urllib.request


class _ObservedConnectionMixin:
    def __init__(self, *args, trace, **kwargs):
        self._trace = trace
        self._inside_connect = False
        super().__init__(*args, **kwargs)

    def request(self, *args, **kwargs):
        result = super().request(*args, **kwargs)
        self._trace.request_complete()
        return result

    def send(self, data):
        if self.sock is None:
            self.connect()
        if self._inside_connect:
            return super().send(data)
        self._trace.request_send_started()
        self.sock.settimeout(self._trace.remaining())
        try:
            return super().send(data)
        except TimeoutError:
            raise self._trace.timeout_error('write') from None

    def getresponse(self):
        self.sock.settimeout(self._trace.remaining())
        try:
            response = super().getresponse()
        except TimeoutError:
            raise self._trace.timeout_error() from None
        sock = getattr(getattr(response, 'fp', None), 'raw', None)
        sock = getattr(sock, '_sock', None) or self.sock
        if sock is not None:
            self._trace.track_socket(sock)
        self._trace.headers_received(response.headers)
        return response


class _ObservedHTTPConnection(_ObservedConnectionMixin, http.client.HTTPConnection):
    def connect(self):
        started = self._trace.begin('connect')
        self.timeout = self._trace.remaining()
        self._inside_connect = True
        try:
            http.client.HTTPConnection.connect(self)
            self._trace.track_socket(self.sock)
        except TimeoutError:
            raise self._trace.timeout_error('connect') from None
        finally:
            self._inside_connect = False
            self._trace.complete('connect', started, 'connect_ms')


class _ObservedHTTPSConnection(_ObservedConnectionMixin, http.client.HTTPSConnection):
    def connect(self):
        started = self._trace.begin('connect')
        self.timeout = self._trace.remaining()
        self._inside_connect = True
        try:
            http.client.HTTPConnection.connect(self)
            self._trace.track_socket(self.sock)
        except TimeoutError:
            raise self._trace.timeout_error('connect') from None
        finally:
            self._inside_connect = False
            self._trace.complete('connect', started, 'connect_ms')
        server_hostname = self._tunnel_host or self.host
        started = self._trace.begin('tls')
        self.sock.settimeout(self._trace.remaining())
        try:
            self.sock = self._context.wrap_socket(
                self.sock, server_hostname=server_hostname,
                do_handshake_on_connect=False)
            self._trace.track_socket(self.sock)
            self.sock.settimeout(self._trace.remaining())
            self.sock.do_handshake()
        except TimeoutError:
            raise self._trace.timeout_error('tls') from None
        finally:
            self._trace.complete('tls', started, 'tls_ms')


class _ObservedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, trace):
        super().__init__()
        self.trace = trace

    def http_open(self, request):
        factory = functools.partial(_ObservedHTTPConnection, trace=self.trace)
        return self.do_open(factory, request)


class _ObservedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, trace, context):
        super().__init__(context=context)
        self.trace = trace

    def https_open(self, request):
        factory = functools.partial(_ObservedHTTPSConnection, trace=self.trace)
        return self.do_open(factory, request, context=self._context)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _open_response(request, trace):
    context = ssl.create_default_context()
    context.set_alpn_protocols(['http/1.1'])
    opener = urllib.request.build_opener(
        _NoRedirect(), _ObservedHTTPHandler(trace),
        _ObservedHTTPSHandler(trace, context))
    return opener.open(request, timeout=trace.remaining())


def _response_socket(response):
    return getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)


def _prepare_http_error(error, request, trace):
    """Capture bounded diagnostics while the writer deadline still owns I/O."""
    sock = _response_socket(getattr(error, 'fp', None))
    if sock is not None:
        trace.track_socket(sock)
        sock.settimeout(trace.remaining())
    authorization = request.get_header('Authorization', '')
    secret = authorization[7:] if authorization.startswith('Bearer ') else None
    # Imported lazily to avoid a module cycle at import time. call_json has
    # finished loading before any transport worker can run.
    from .model import _capture_http_error
    error._travel_diagnostic = _capture_http_error(error, secret)
    try:
        error.close()
    except Exception:
        # The known HTTP status remains authoritative; close failures are
        # diagnostic cleanup failures and must not replace classification.
        pass
