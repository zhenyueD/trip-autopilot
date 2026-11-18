"""Offline real-HTTP checks; the upstream is a recording fake, never a model."""
import contextlib
import http.client
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import re
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

MODULE = Path(__file__).resolve().parents[1] / "tools" / "serve_customer_preview.py"
SPEC = importlib.util.spec_from_file_location("customer_preview", MODULE)
gateway = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gateway
SPEC.loader.exec_module(gateway)

PUBLIC_HOST = "preview.example.test"
PASSWORD = "offline-test-only-strong-password-1234"


class RecordingHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self._record()

    def do_POST(self):
        self._record()

    def _record(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        record = {"path": self.path, "headers": dict(self.headers),
                  "body": body.decode(), "method": self.command}
        self.server.records.append(record)
        data = json.dumps(record).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@contextlib.contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class GatewayHTTPTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.upstream = self.stack.enter_context(running(
            ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)))
        self.upstream.records = []
        self.config = gateway.Config(password=PASSWORD, public_host=PUBLIC_HOST,
                                     backend_port=self.upstream.server_port)
        self.server = self.stack.enter_context(running(gateway.create_server(self.config, port=0)))

    def request(self, method="GET", path="/api/agent/trips", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {"Host": PUBLIC_HOST, **(headers or {})}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.status, response.getheaders(), response.read()
        connection.close()
        return result

    def login_form(self):
        status, headers, body = self.request(path="/__preview/login")
        self.assertEqual(status, 200)
        # no-referrer turns browser form POST Origin into null; keep same-origin
        # login usable without relaxing the server's Origin check.
        self.assertEqual(dict(headers)['Referrer-Policy'], 'same-origin')
        csrf = re.search(rb'name="csrf" value="([^"]+)"', body).group(1).decode()
        cookie = SimpleCookie()
        for key, value in headers:
            if key.lower() == "set-cookie":
                cookie.load(value)
        return csrf, "; ".join(f"{key}={item.value}" for key, item in cookie.items())

    def test_authenticated_root_uses_current_studio_not_legacy_index(self):
        cookie = self.auth_cookie()
        for path in ('/', '/index.html'):
            status, headers, _ = self.request(path=path, headers={'Cookie': cookie})
            self.assertEqual(status, 303)
            self.assertEqual(dict(headers)['Location'], '/studio.html')
        self.assertEqual(self.upstream.records, [])

    def login(self, password=PASSWORD, *, origin=None):
        csrf, cookie = self.login_form()
        headers = {"Origin": origin or "https://" + PUBLIC_HOST,
                   "Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie}
        return self.request("POST", "/__preview/login",
                            urlencode({"password": password, "csrf": csrf}), headers)

    def auth_cookie(self):
        status, headers, _ = self.login()
        self.assertEqual(status, 303)
        cookies = SimpleCookie()
        for key, value in headers:
            if key.lower() == "set-cookie":
                cookies.load(value)
        cookie = cookies[gateway.AUTH_COOKIE]
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        self.assertEqual(cookie["path"], "/")
        self.assertEqual(dict(headers)["Location"], "/studio.html")
        return f"{gateway.AUTH_COOKIE}={cookie.value}"

    def test_unauthorized_api_static_and_download_never_reach_upstream(self):
        for path in ("/api/agent/trips", "/assets/app.js", "/reader.pdf", "/secret.txt"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path=path)[0], 401)
        self.assertEqual(self.upstream.records, [])

    def test_login_cookie_and_bearer_host_origin_translation(self):
        cookie = self.auth_cookie()
        status, _, body = self.request("POST", "/api/agent/trips?view=1", '{"ok":true}', {
            "Cookie": cookie, "Authorization": "Bearer product-capability",
            "Origin": "https://" + PUBLIC_HOST, "Content-Type": "application/json",
            "X-Forwarded-Host": "malicious.example", "Forwarded": "host=evil"})
        self.assertEqual(status, 200)
        recorded = json.loads(body)
        headers = {k.lower(): v for k, v in recorded["headers"].items()}
        self.assertEqual(headers["authorization"], "Bearer product-capability")
        self.assertEqual(headers["host"], f"127.0.0.1:{self.upstream.server_port}")
        self.assertEqual(headers["origin"], "http://" + headers["host"])
        self.assertNotIn("cookie", headers)
        self.assertNotIn("forwarded", headers)
        self.assertNotIn("x-forwarded-host", headers)
        self.assertEqual(recorded["body"], '{"ok":true}')

    def test_cross_origin_missing_origin_and_fetch_site_posts_are_denied(self):
        cookie = self.auth_cookie()
        for extra in ({"Origin": "https://attacker.example"}, {},
                      {"Origin": "null"},
                      {"Origin": "https://" + PUBLIC_HOST, "Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.request("POST", headers={"Cookie": cookie, **extra})[0], 403)
        self.assertEqual(self.upstream.records, [])

    def test_wrong_password_expired_and_tampered_cookies_are_denied(self):
        self.assertEqual(self.login(password="incorrect")[0], 401)
        cookie = self.auth_cookie()
        self.assertEqual(self.request(headers={"Cookie": cookie + "bad"})[0], 401)
        with patch.object(gateway.time, "time", return_value=time.time() + 90000):
            self.assertEqual(self.request(headers={"Cookie": cookie})[0], 401)
        self.assertEqual(self.upstream.records, [])

    def test_login_csrf_missing_mismatch_and_cross_origin_are_denied(self):
        csrf, cookie = self.login_form()
        valid_body = urlencode({"password": PASSWORD, "csrf": csrf})
        base = {"Origin": "https://" + PUBLIC_HOST,
                "Content-Type": "application/x-www-form-urlencoded"}
        self.assertEqual(self.request("POST", "/__preview/login", valid_body, base)[0], 403)
        self.assertEqual(self.request("POST", "/__preview/login",
            urlencode({"password": PASSWORD, "csrf": "bad"}), {**base, "Cookie": cookie})[0], 403)
        self.assertEqual(self.login(origin="https://attacker.example")[0], 403)
        self.assertEqual(self.upstream.records, [])

    def test_expired_login_form_and_missing_login_origin_are_denied(self):
        csrf, cookie = self.login_form()
        body = urlencode({"password": PASSWORD, "csrf": csrf})
        headers = {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"}
        self.assertEqual(self.request("POST", "/__preview/login", body, headers)[0], 403)
        with patch.object(gateway.time, "time", return_value=time.time() + gateway.CSRF_SECONDS + 1):
            headers["Origin"] = "https://" + PUBLIC_HOST
            self.assertEqual(self.request("POST", "/__preview/login", body, headers)[0], 403)
        self.assertEqual(self.upstream.records, [])

    def test_login_rate_limit_is_global_not_forwarded_header_dependent(self):
        for _ in range(gateway.LOGIN_ATTEMPTS):
            self.assertEqual(self.login(password="wrong")[0], 401)
        status, headers, _ = self.login()
        self.assertEqual(status, 429)
        self.assertIn("Retry-After", dict(headers))
        self.assertEqual(self.upstream.records, [])

    def test_untrusted_host_cannot_be_replaced_by_forwarded_host(self):
        status, _, _ = self.request(path="/__preview/login", headers={
            "Host": "attacker.example", "X-Forwarded-Host": PUBLIC_HOST})
        self.assertEqual(status, 403)

    def test_local_host_remains_usable(self):
        cookie = self.auth_cookie()
        local_host = f"127.0.0.1:{self.server.server_port}"
        self.assertEqual(self.request(headers={"Host": local_host, "Cookie": cookie})[0], 200)

    def test_proxy_target_and_traversal_are_rejected(self):
        cookie = self.auth_cookie()
        for path in ("http://evil.example/", "//evil.example/", "/../secret",
                     "/%2e%2e/secret", "/%252e%252e/secret", "/a%5c..%5csecret",
                     "/%00bad", "/%2f%2fevil.example"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path=path, headers={"Cookie": cookie})[0], 400)
        self.assertEqual(self.upstream.records, [])

    def test_oversized_and_chunked_requests_rejected_before_upstream(self):
        headers = {"Cookie": self.auth_cookie(), "Origin": "https://" + PUBLIC_HOST}
        self.assertEqual(self.request("POST", headers={
            **headers, "Content-Length": str(gateway.MAX_BODY + 1)})[0], 413)
        self.assertEqual(self.request("POST", headers={
            **headers, "Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.upstream.records, [])

    def test_duplicate_security_headers_and_negative_length_are_rejected(self):
        cookie = self.auth_cookie()
        for duplicate in ("Host", "Origin", "Authorization", "Cookie", "Content-Length"):
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
            connection.putrequest("POST", "/api/agent/trips", skip_host=True)
            headers = {"Host": PUBLIC_HOST, "Origin": "https://" + PUBLIC_HOST,
                       "Authorization": "Bearer test", "Cookie": cookie, "Content-Length": "0"}
            for key, value in headers.items():
                connection.putheader(key, value)
            connection.putheader(duplicate, headers[duplicate])
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            connection.close()
        self.assertEqual(self.request("POST", headers={"Cookie": cookie,
            "Origin": "https://" + PUBLIC_HOST, "Content-Length": "-1"})[0], 400)
        self.assertEqual(self.upstream.records, [])

    def test_html_login_and_head_do_not_expose_application(self):
        status, _, body = self.request(path="/studio.html")
        self.assertEqual(status, 401)
        self.assertIn("验收口令".encode(), body)
        self.assertNotIn(PASSWORD.encode(), body)
        status, _, body = self.request("HEAD", "/studio.html")
        self.assertEqual(status, 401)
        self.assertEqual(body, b"")
        self.assertEqual(self.upstream.records, [])

    def test_invalid_login_form_and_method_fail_closed(self):
        headers = {"Origin": "https://" + PUBLIC_HOST}
        self.assertEqual(self.request("POST", "/__preview/login", "", headers)[0], 415)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        self.assertEqual(self.request("POST", "/__preview/login", "invalid", headers)[0], 400)
        self.assertEqual(self.request("PUT", "/__preview/login", "", headers)[0], 405)
        self.assertEqual(self.request("POST", "/__preview/login", None, {
            **headers, "Content-Length": str(gateway.MAX_LOGIN_BODY + 1)})[0], 413)
        self.assertEqual(self.upstream.records, [])

    def test_upstream_unavailable_is_sanitized_502(self):
        cookie = self.auth_cookie()
        self.upstream.shutdown()
        self.upstream.server_close()
        status, _, body = self.request(headers={"Cookie": cookie,
                                               "Authorization": "Bearer very-private-token"})
        self.assertEqual(status, 502)
        self.assertNotIn(PASSWORD.encode(), body)
        self.assertNotIn(b"very-private-token", body)
        self.assertNotIn(b"127.0.0.1", body)
        self.assertNotIn(b"Traceback", body)


class ConfigTests(unittest.TestCase):
    def test_config_rejects_short_password_invalid_host_and_excessive_expiry(self):
        base = {"password": PASSWORD, "public_host": PUBLIC_HOST, "backend_port": 7393}
        for update in ({"password": ""}, {"password": "short"},
                       {"public_host": "https://evil.example/path"},
                       {"public_host": "evil.example:443"}, {"public_host": "127.0.0.1"},
                       {"backend_port": 0}, {"session_seconds": 86401}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                gateway.Config(**{**base, **update})

    def test_environment_password_is_required_and_not_cli_argument(self):
        with patch.dict(gateway.os.environ, {}, clear=True), self.assertRaises(SystemExit):
            gateway.main(["--public-host", PUBLIC_HOST, "--backend-port", "7393"])

    def test_signing_key_restart_invalidates_session(self):
        config = gateway.Config(password=PASSWORD, public_host=PUBLIC_HOST, backend_port=7393)
        first = gateway.create_server(config, port=0)
        second = gateway.create_server(config, port=0)
        try:
            token = first.sign("session", config.session_seconds)
            self.assertTrue(first.valid("session", token, config.session_seconds))
            self.assertFalse(second.valid("session", token, config.session_seconds))
            self.assertNotIn(PASSWORD, repr(config))
            with self.assertRaises(ValueError):
                gateway.create_server(config, port=7393)
        finally:
            first.server_close()
            second.server_close()


if __name__ == "__main__":
    unittest.main()
