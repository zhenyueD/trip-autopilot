#!/usr/bin/env python3
"""Password gate for a temporary HTTPS tunnel to a fixed loopback service.

Set TRIP_PREVIEW_PASSWORD in the process environment (at least 24 characters).
Start the tunnel to this server, then supply its exact hostname with --public-host.
The tunnel must preserve Host. No forwarded header is a source of authority.
Sessions use a process-local signing key and expire on restart or within 24 hours.
The service stays on 127.0.0.1; this adapter does not provide TLS itself.
"""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, field
import hashlib
import hmac
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
import sys
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.material_limits import MAX_REQUEST_BYTES, MAX_TEXT_REQUEST_BYTES, UPLOAD_LIMITS

AUTH_COOKIE = "__Host-trip_preview_auth"
CSRF_COOKIE = "__Host-trip_preview_csrf"
LOGIN_PATH = "/__preview/login"
MAX_BODY = MAX_REQUEST_BYTES
MAX_LOGIN_BODY = 4096
MAX_RESPONSE = 64 * 1024 * 1024
LOGIN_ATTEMPTS = 8
LOGIN_WINDOW = 60
CSRF_SECONDS = 600
HOP_HEADERS = frozenset({"connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"})
COOKIE_OCTETS = r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]*"
COOKIE_PAIR = re.compile(r"([!#$%&'*+\-.^_`|~0-9A-Za-z]+)=(" + COOKIE_OCTETS + r'|"' + COOKIE_OCTETS + r'")')


@dataclass(frozen=True)
class Config:
    password: str = field(repr=False)
    public_host: str
    backend_port: int
    session_seconds: int = 8 * 3600

    def __post_init__(self):
        if len(self.password) < 24 or len(self.password.encode()) > 1024:
            raise ValueError("TRIP_PREVIEW_PASSWORD requires at least 24 characters and at most 1024 UTF-8 bytes")
        if not 1 <= self.backend_port <= 65535:
            raise ValueError("backend port must be between 1 and 65535")
        if not 1 <= self.session_seconds <= 86400:
            raise ValueError("session lifetime must be between 1 and 86400 seconds")
        labels = self.public_host.split(".")
        label_pattern = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        if (len(self.public_host) > 253 or len(labels) < 2
                or not all(re.fullmatch(label_pattern, label) for label in labels)):
            raise ValueError("public host must be an exact lowercase DNS hostname, without scheme or port")
        try:
            ipaddress.ip_address(self.public_host)
        except ValueError:
            return
        raise ValueError("public host must be a DNS hostname")


class PreviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config, port):
        self.config = config
        self.signing_key = secrets.token_bytes(32)
        self.login_attempts = deque()
        self.login_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), PreviewHandler)

    def sign(self, purpose, lifetime):
        payload = f"{int(time.time()) + lifetime}.{secrets.token_hex(16)}"
        signature = hmac.new(self.signing_key, f"{purpose}:{payload}".encode(), hashlib.sha256)
        return f"{payload}.{signature.hexdigest()}"

    def valid(self, purpose, token, lifetime):
        if not re.fullmatch(r"[0-9]{1,12}\.[0-9a-f]{32}\.[0-9a-f]{64}", token):
            return False
        expiry, nonce, signature = token.split(".")
        payload = f"{purpose}:{expiry}.{nonce}".encode()
        expected = hmac.new(self.signing_key, payload, hashlib.sha256).hexdigest()
        now = int(time.time())
        return hmac.compare_digest(expected, signature) and now < int(expiry) <= now + lifetime

    def allow_login(self):
        # One bounded global bucket: tunnel clients share a loopback address and
        # user-supplied CF-/X-Forwarded-* headers must not bypass the limiter.
        now = time.monotonic()
        with self.login_lock:
            while self.login_attempts and self.login_attempts[0] <= now - LOGIN_WINDOW:
                self.login_attempts.popleft()
            if len(self.login_attempts) >= LOGIN_ATTEMPTS:
                return False
            self.login_attempts.append(now)
            return True


def valid_target(target):
    if not target.startswith("/") or target.startswith("//") or "#" in target:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in target):
        return False
    try:
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc:
            return False
        path = parsed.path
        for _ in range(4):
            decoded = unquote(path, errors="strict")
            if decoded == path:
                break
            path = decoded
        else:
            return False
        return (not path.startswith("//") and not any(char in path for char in "\\?#")
                and not any(ord(char) < 32 or ord(char) == 127 for char in path)
                and not any(part in {".", ".."} for part in path.split("/")))
    except (ValueError, UnicodeError):
        return False


class PreviewHandler(BaseHTTPRequestHandler):
    server_version = "PreviewGateway"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_):
        pass  # URLs, passwords, cookies and product tokens never enter access logs.

    def send_error(self, code, message=None, explain=None):
        self._error(code, "请求无法处理。")

    def _reply(self, status, body=b"", content_type="application/json; charset=utf-8", headers=(),
               referrer_policy="no-referrer"):
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", referrer_policy)
        self.send_header("Connection", "close")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status, message, headers=()):
        self._reply(status, json.dumps({"error": message}, ensure_ascii=False).encode(), headers=headers)

    def _cookie(self, name):
        return self._request_cookies.get(name, "")

    def _validate_cookies(self):
        # RFC 6265 cookie-pair grammar; one parse serves uniqueness and auth.
        # Split fields have semicolon semantics (RFC 9113), never comma semantics.
        self._request_cookies = {}
        fields = self.headers.get_all("Cookie", [])
        for raw in fields:
            for part in raw.split(";"):
                pair = COOKIE_PAIR.fullmatch(part.strip(" \t"))
                if pair is None:
                    self._reject_headers("invalid_cookie_syntax", "cookie", len(fields))
                    return False
                name, value = pair.groups()
                if name in {AUTH_COOKIE, CSRF_COOKIE} and name in self._request_cookies:
                    self._reject_headers("duplicate_gateway_cookie", "cookie", 2)
                    return False
                self._request_cookies[name] = value[1:-1] if value.startswith('"') else value
        return True

    @staticmethod
    def _set_cookie(name, token, lifetime):
        return ("Set-Cookie", f"{name}={token}; Path=/; Max-Age={lifetime}; HttpOnly; Secure; SameSite=Strict")

    def _login_page(self, status=200, message="请输入验收口令，继续使用旅行规划。"):
        csrf = self.server.sign("csrf", CSRF_SECONDS)
        body = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>旅行规划 · 验收入口</title>
<style>body{{margin:0;background:#f5f5f1;color:#242a29;font:17px system-ui;display:grid;min-height:100vh;place-items:center}}
main{{max-width:340px;padding:32px}}h1{{font-size:25px}}p{{line-height:1.7;color:#59615e}}
input,button{{box-sizing:border-box;width:100%;font:inherit;border-radius:8px;padding:13px;margin-top:12px}}
input{{border:1px solid #b7c0b9}}button{{background:#234d40;color:white;border:0;cursor:pointer}}</style>
<main><h1>旅行规划</h1><p>{message}</p><form method="post" action="{LOGIN_PATH}">
<label for="password">验收口令</label><input id="password" type="password" name="password" required
 autocomplete="current-password" maxlength="1024" autofocus>
<input type="hidden" name="csrf" value="{csrf}"><button type="submit">进入</button></form></main></html>'''.encode()
        policy = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        self._reply(status, body, "text/html; charset=utf-8", (
            self._set_cookie(CSRF_COOKIE, csrf, CSRF_SECONDS), ("Content-Security-Policy", policy)),
            referrer_policy="same-origin")

    def _reject_headers(self, reason, field, count):
        diagnostic_id = secrets.token_hex(8)
        known_fields = {"host", "origin", "authorization", "cookie", "content-length",
                        "content-type", "transfer-encoding"}
        proxy_fields = ("forwarded", "x-forwarded-for", "x-forwarded-proto", "cf-ray",
                        "cf-connecting-ip", "cf-visitor")
        event = {"event": "preview_header_rejected", "diagnostic_id": diagnostic_id,
                 "reason": reason, "field": field.lower() if field.lower() in known_fields else "other",
                 "count": count,
                 "http_version": self.request_version if self.request_version in {"HTTP/1.0", "HTTP/1.1"} else "other",
                 "proxy_field_counts": {name: len(self.headers.get_all(name, [])) for name in proxy_fields}}
        # No URL, method text, header value, cookie value, IP or credential is logged.
        print(json.dumps(event, separators=(",", ":")), file=sys.stderr, flush=True)
        self._error(400, "请求头格式无效。", (("X-Preview-Diagnostic-Id", diagnostic_id),))

    def _request_context(self):
        for key in ("Host", "Origin", "Authorization", "Content-Length", "Content-Type"):
            count = len(self.headers.get_all(key, []))
            if count > 1:
                self._reject_headers("duplicate_sensitive_header", key, count)
                return None
        # Reject presence, including an empty first value masking a second field.
        encodings = self.headers.get_all("Transfer-Encoding", [])
        if encodings:
            self._reject_headers("unsupported_transfer_encoding", "transfer-encoding", len(encodings))
            return None
        for key, value in self.headers.items():
            if "\r" in value or "\n" in value:
                self._reject_headers("header_line_break", key, len(self.headers.get_all(key, [])))
                return None
        if not self._validate_cookies():
            return None
        local_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        host = self.headers.get("Host", "").lower()
        if host != self.server.config.public_host and host not in local_hosts:
            self._error(403, "访问地址无效。")
            return None
        expected_origin = ("http://" if host in local_hosts else "https://") + host
        origin = self.headers.get("Origin")
        mutating = self.command not in {"GET", "HEAD", "OPTIONS"}
        if ((origin is not None and origin != expected_origin)
                or (mutating and (origin is None or self.headers.get("Sec-Fetch-Site") == "cross-site"))):
            self._error(403, "请求来源校验失败。")
            return None
        target = self.requestline.split()[1]
        if not valid_target(target):
            self._error(400, "请求路径无效。")
            return None
        return target

    def _read_body(self, maximum):
        length = self.headers.get("Content-Length", "0")
        if not re.fullmatch(r"[0-9]{1,10}", length):
            self._error(400, "请求长度无效。")
            return None
        if int(length) > maximum:
            self._error(413, "请求内容过大。")
            return None
        try:
            body = self.rfile.read(int(length))
            if len(body) != int(length):
                raise ValueError("incomplete request")
            return body
        except (OSError, ValueError):
            self._error(400, "请求内容不完整。")
            return None

    def _login(self):
        if not self.server.allow_login():
            self._error(429, "尝试次数过多，请稍后重试。", (("Retry-After", str(LOGIN_WINDOW)),))
            return
        if self.headers.get_content_type() != "application/x-www-form-urlencoded":
            self._error(415, "登录表单格式无效。")
            return
        body = self._read_body(MAX_LOGIN_BODY)
        if body is None:
            return
        try:
            form = parse_qs(body.decode("utf-8"), strict_parsing=True, max_num_fields=2)
        except (ValueError, UnicodeError):
            self._error(400, "登录表单格式无效。")
            return
        csrf = self._cookie(CSRF_COOKIE)
        submitted = form.get("csrf", [""])
        if (len(submitted) != 1 or not hmac.compare_digest(csrf.encode(), submitted[0].encode())
                or not self.server.valid("csrf", csrf, CSRF_SECONDS)):
            self._error(403, "登录页面已失效，请刷新后重试。")
            return
        password = form.get("password", [""])
        if len(password) != 1 or not hmac.compare_digest(password[0].encode(), self.server.config.password.encode()):
            self._login_page(401, "口令不正确，请重试。")
            return
        session = self.server.sign("session", self.server.config.session_seconds)
        self._reply(303, headers=(("Location", "/studio.html"),
            self._set_cookie(AUTH_COOKIE, session, self.server.config.session_seconds),
            self._set_cookie(CSRF_COOKIE, "", 0)))

    def _upstream_headers(self, body):
        connection_headers = {item.strip().lower() for item in self.headers.get("Connection", "").split(",")}
        excluded = HOP_HEADERS | connection_headers | {"host", "origin", "cookie", "content-length", "forwarded"}
        headers = {key: value for key, value in self.headers.items()
                   if key.lower() not in excluded and not key.lower().startswith(("x-forwarded-", "cf-"))}
        host = f"127.0.0.1:{self.server.config.backend_port}"
        headers.update({"Host": host, "Origin": "http://" + host, "Connection": "close"})
        if body or self.command not in {"GET", "HEAD"}:
            headers["Content-Length"] = str(len(body))
        return headers

    def _proxy(self, target, body):
        upstream = HTTPConnection("127.0.0.1", self.server.config.backend_port, timeout=120)
        try:
            upstream.request(self.command, target, body=body, headers=self._upstream_headers(body))
            response = upstream.getresponse()
            data = response.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                raise ValueError("response limit")
            if response.status == 200 and urlsplit(target).path == "/api/agent/capabilities":
                capability = json.loads(data)
                if isinstance(capability, dict):
                    limits = dict(UPLOAD_LIMITS)
                    if capability.get("attachments") is False:
                        limits["request_body_bytes"] = MAX_TEXT_REQUEST_BYTES
                    capability["upload_limits"] = limits
                    data = json.dumps(capability, ensure_ascii=False).encode()
            connection_headers = {item.strip().lower() for item in response.getheader("Connection", "").split(",")}
            excluded = HOP_HEADERS | connection_headers | {"content-length", "content-type", "cache-control",
                "server", "date", "set-cookie", "x-content-type-options", "referrer-policy"}
            headers = [(key, value) for key, value in response.getheaders() if key.lower() not in excluded]
            self._reply(response.status, data, response.getheader("Content-Type", "application/octet-stream"), headers)
        except (OSError, HTTPException, ValueError):
            self._error(502, "服务暂时不可用，请稍后重试。")
        finally:
            upstream.close()

    def _handle(self):
        target = self._request_context()
        if target is None:
            return
        path = urlsplit(target).path
        if path == LOGIN_PATH:
            if self.command in {"GET", "HEAD"}:
                self._login_page()
            elif self.command == "POST":
                self._login()
            else:
                self._error(405, "请求方法不支持。")
            return
        if not self.server.valid("session", self._cookie(AUTH_COOKIE), self.server.config.session_seconds):
            if self.command in {"GET", "HEAD"} and path in {"/", "/studio.html"}:
                self._login_page(401)
            else:
                self._error(401, "请先通过验收口令登录。")
            return
        if self.command in {"GET", "HEAD"} and path in {"/", "/index.html"}:
            self._reply(303, headers=(("Location", "/studio.html"),))
            return
        body = self._read_body(MAX_BODY)
        if body is not None:
            self._proxy(target, body)

    do_GET = do_HEAD = do_POST = do_PATCH = do_PUT = do_DELETE = do_OPTIONS = _handle


def create_server(config, port=7394):
    if port and port == config.backend_port:
        raise ValueError("gateway and backend ports must differ")
    return PreviewServer(config, port)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-host", required=True)
    parser.add_argument("--backend-port", type=int, required=True)
    parser.add_argument("--listen-port", type=int, default=7394)
    parser.add_argument("--session-seconds", type=int, default=28800)
    args = parser.parse_args(argv)
    try:
        config = Config(os.environ.get("TRIP_PREVIEW_PASSWORD", ""), args.public_host,
                        args.backend_port, args.session_seconds)
        server = create_server(config, args.listen_port)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Preview gateway could not start: {type(exc).__name__}. Check configuration.\n")
    print(f"Preview gateway listening on 127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
