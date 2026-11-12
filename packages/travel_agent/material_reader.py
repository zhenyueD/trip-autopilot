"""Bounded, credential-free public text reader. Every fetched claim is untrusted.

No browser, JavaScript, cookies, proxy, image retrieval or factual verification.
The local injection patterns are alarms, not a proof that other text is safe.
"""

from datetime import datetime, timezone
from email.message import Message
import hashlib
from html.parser import HTMLParser
import http.client
import ipaddress
import queue
import re
import socket
import ssl
import threading
import time
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit


MAX_LINKS = 5
MAX_URL_CHARS = 2048
MAX_BODY_BYTES = 262144
MAX_TEXT_CHARS = 8000
MAX_SOCIAL_EXCERPT_CHARS = 200
_REDDIT_POST_PATH = re.compile(
    r'/r/[A-Za-z0-9_]{1,21}/comments/[a-z0-9]+(?:/[A-Za-z0-9_~-]+)?(?:/[a-z0-9]+)?/?')
MAX_REDIRECTS = 3
MAX_HTML_DEPTH = 256
TOTAL_TIMEOUT_SECONDS = 8.0
_WORKERS = threading.BoundedSemaphore(4)
_LINK_PATTERN = re.compile(r"https?://[^\s<>\"'`【】（），。！？；：、￥¥]+", re.I)
_SENSITIVE_QUERY = re.compile(
    r"password|passwd|(?:^|[?&#;])pwd=|secret|token|session|auth|credential|"
    r"api.?key|login|sign.?in|order|booking|confirmation|reservation|"
    r"密码|口令|订单|验证码|登录|授权|凭证", re.I)
_PRIVATE_PATH = re.compile(
    r"(?:^|/)(?:accounts?|my-account|orders?|login|log-in|sign-in|signin|signout|"
    r"auth|oauth|reset-password|password-reset|reservations?|confirmations?)(?:/|$)|"
    r"(?:^|/)bookings?/(?:manage|view|details?|[a-z0-9_-]{16,})(?:/|$)", re.I)
# Adapted from prompt-injection-shield/templates/shield.ts, locally only.
# A non-match conveys no security verdict; all returned data remains untrusted.
_INJECTION_PATTERNS = tuple(re.compile(pattern, re.I) for pattern in (
    r"ignore\s+(all\s+)?(previous|prior|earlier)\s+instructions?",
    r"(reveal|show|print|output|disclose|leak|repeat)\s+(your\s+)?(system\s+prompt|instructions|the\s+prompt)",
    r"\b(jailbreak|dan\s+mode|developer\s+mode\s+enabled|do\s+anything\s+now)\b",
    r"disregard\s+(everything|all|previous|prior|the\s+above)",
    r"you\s+are\s+now\s+(a\s+)?[\w-]+",
    r"mark\s+(this|the)\s+lead\s+(as\s+)?priority\s+\d{3,}",
    r"override\s+(your\s+)?(role|persona|instructions|directives)",
    r"reset\s+(your\s+)?(memory|instructions|context|prompt)",
    r"act\s+as\s+(if\s+you\s+(are|were)|a\s+different)",
    r"forget\s+(everything|all\s+previous|what\s+i\s+told\s+you)",
    r"new\s+instructions?\s*:[\s\S]{0,200}(send|email|call|book|approve|reveal)",
    r"(?:忽略|无视|忘记|覆盖|重置).{0,30}(?:指令|提示词|角色|系统|规则|上下文)",
    r"(?:输出|泄露|显示|打印|透露).{0,20}(?:系统提示|系统指令|密码|密钥|令牌)",
    r"你(?:现在|从现在起).{0,15}(?:扮演|是|成为).{0,10}(?:管理员|助手|模型|系统)",
    r"(?:把|将).{0,40}(?:用户|订单|密码|密钥|个人信息).{0,30}(?:发送|上传|转发|泄露)",
))
_GATE = re.compile(
    r"enable javascript|javascript (?:is required|must be enabled)|"
    r"(?:please|must|need to) (?:log ?in|sign ?in)|log ?in to (?:view|continue)|"
    r"verify (?:that )?you are human|checking your browser|captcha|access denied|"
    r"just a moment|blocked by network security|"
    r"(?:open|continue|view|read).{0,20}in (?:the )?(?:reddit )?app to (?:view|read|continue)|"
    r"(?:only available|must be (?:viewed|read)) in (?:the )?(?:reddit )?app|"
    r"(?:download|install) (?:the )?(?:reddit )?app to (?:view|read|continue)|"
    r"请.{0,8}登录|登录后.{0,8}(?:查看|访问)|(?:打开|下载).{0,8}App.{0,8}(?:查看|阅读)|"
    r"验证码|安全验证|访问受限", re.I)


def _clean_link(value):
    value = value.strip().rstrip(".,!?;:，。！？；：、")
    while value.endswith(")") and value.count(")") > value.count("("):
        value = value[:-1]
    while value.endswith("]") and value.count("]") > value.count("["):
        value = value[:-1]
    return value


def extract_links(texts, explicit=None) -> list[str]:
    """Find at most five unique HTTP(S) URLs; HTTP is accepted but not fetched."""
    texts = [texts] if isinstance(texts, str) else texts
    if not isinstance(texts, (list, tuple)) or any(not isinstance(x, str) for x in texts):
        raise ValueError("texts must be text or a list of text")
    if explicit is not None and (not isinstance(explicit, list)
                                 or any(not isinstance(x, str) for x in explicit)):
        raise ValueError("links must be a list of HTTP(S) URLs")
    found = [_clean_link(match.group()) for text in texts
             for match in _LINK_PATTERN.finditer(text)]
    for value in explicit or []:
        value = _clean_link(value)
        if not _LINK_PATTERN.fullmatch(value):
            raise ValueError("links must contain HTTP(S) URLs only")
        found.append(value)
    unique = list(dict.fromkeys(found))
    if len(unique) > MAX_LINKS:
        raise ValueError("At most 5 public links may be submitted")
    if any(len(value) > MAX_URL_CHARS for value in unique):
        raise ValueError("Public URLs must be at most 2048 characters")
    return unique


def _public_ip(value):
    """Reject special networks, scoped IPv6 and embedded private IPv4 forms."""
    if "%" in value:
        return False
    address = ipaddress.ip_address(value)
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        embedded = address.ipv4_mapped or address.sixtofour
        if embedded is not None and not _public_ip(str(embedded)):
            return False
        if address.teredo is not None:
            return False
    return True


def _decoded(value):
    for _ in range(4):
        decoded = unquote(value)
        if decoded == value:
            return value
        value = decoded
    # Deeply nested encodings are ambiguous and unnecessary for public evidence.
    if unquote(value) != value:
        raise ValueError("Nested URL encoding is not supported")
    return value


def _validate_host(host):
    if not host or "%" in host or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Only public internet hosts are allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # inet_aton accepts legacy forms (127.1, decimal, hex and octal) without DNS.
        try:
            socket.inet_aton(host)
        except OSError:
            if not re.fullmatch(r"(?=.{1,253}$)[a-z0-9-]+(?:\.[a-z0-9-]+)+", host):
                raise ValueError("Public host is malformed") from None
            return
        raise ValueError("Legacy IP encoding is not allowed") from None
    if not _public_ip(str(address)):
        raise ValueError("Non-public network destination")


def _reddit_host(host):
    return host == 'reddit.com' or host.endswith('.reddit.com') or host in {'redd.it', 'www.redd.it'}


def _validate_url(url):
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        raise ValueError("Invalid public URL")
    decoded = _decoded(url)
    if ("\\" in decoded or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
            or any(ord(char) < 33 or ord(char) == 127 for char in url)):
        raise ValueError("URL contains control characters or ambiguous separators")
    parsed = urlsplit(url)
    # Percent-encoded spaces are valid inside public path segments (buy%20ticket),
    # not in authority/query/fragment or at segment edges. Keep the encoded path
    # on the wire; decoding here is solely for policy inspection.
    path = _decoded(parsed.path)
    if (" " in _decoded(parsed.netloc + parsed.query + parsed.fragment)
            or any(part != part.strip(" ") for part in path.split("/"))):
        raise ValueError("Encoded spaces are only supported inside public path segments")
    if (parsed.scheme != "https" or parsed.port not in (None, 443)
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("Only credential-free HTTPS on port 443 is supported")
    if _SENSITIVE_QUERY.search(_decoded(parsed.query + "&" + parsed.fragment)):
        raise ValueError("Private credentials or account/order links are not fetched")
    if ';' in path or _PRIVATE_PATH.search(path.replace(" ", "")):
        raise ValueError("Private account/order paths are not fetched")
    host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
    _validate_host(host)
    if _reddit_host(host) and (host not in {'reddit.com', 'www.reddit.com'}
            or not _REDDIT_POST_PATH.fullmatch(path) or parsed.query):
        raise ValueError("Only canonical public Reddit post pages are supported")
    netloc = f"[{host}]" if ":" in host else host
    return urlsplit(urlunsplit(("https", netloc, parsed.path or "/", parsed.query, "")))


def _resolve_address(host):
    answers = socket.getaddrinfo(host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
    if not answers or len(answers) > 64:
        raise ValueError("Public host resolution is unavailable")
    for family, socktype, protocol, canonname, address in answers:
        if family not in (socket.AF_INET, socket.AF_INET6) or not _public_ip(address[0]):
            raise ValueError("DNS includes a non-public destination")
    return answers[0]


class _ReadDeadline:
    """One wall-clock budget, including DNS; cancellation closes any live socket.

    OS DNS cannot be interrupted portably. At most four daemon workers may wait
    for it, and a worker checks cancellation before making any connection.
    """

    def __init__(self):
        from .phase1_budget_context import clamp_deadline
        self.end = clamp_deadline(time.monotonic() + TOTAL_TIMEOUT_SECONDS)
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.sock = None

    def remaining(self):
        remaining = self.end - time.monotonic()
        if self.cancelled.is_set() or remaining <= 0:
            raise TimeoutError("Public text read deadline")
        return remaining

    def track(self, sock):
        with self.lock:
            if self.cancelled.is_set():
                sock.close()
                raise TimeoutError("Public text read cancelled")
            self.sock = sock

    def cancel(self):
        with self.lock:
            self.cancelled.set()
            if self.sock is not None:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.sock.close()


def _connect_pinned(parsed, deadline):
    family, socktype, protocol, _, sockaddr = _resolve_address(parsed.hostname)
    deadline.remaining()
    raw = socket.socket(family, socktype, protocol)
    deadline.track(raw)
    try:
        raw.settimeout(deadline.remaining())
        raw.connect(sockaddr)  # Numeric, vetted sockaddr: never resolve host twice.
        context = ssl.create_default_context()
        secured = context.wrap_socket(raw, server_hostname=parsed.hostname,
                                      do_handshake_on_connect=False)
        deadline.track(secured)
        secured.settimeout(deadline.remaining())
        secured.do_handshake()  # Default certificate verification + original host.
        return secured
    except Exception:
        raw.close()
        raise


def _read_body(response, sock, deadline):
    chunks, count = [], 0
    while count < MAX_BODY_BYTES:
        # HTTPResponse closes its file when Content-Length reaches zero. With
        # Connection: close that also releases the socket; don't probe it again.
        if getattr(response, 'length', None) == 0:
            return b"".join(chunks), False
        sock.settimeout(deadline.remaining())
        chunk = response.read1(min(16384, MAX_BODY_BYTES - count))
        if not chunk:
            return b"".join(chunks), False
        chunks.append(chunk)
        count += len(chunk)
    # Do not download byte 262145 simply to detect truncation.
    return b"".join(chunks), True


class _HTTPReadFailure(http.client.HTTPException):
    def __init__(self, response):
        super().__init__('Public page HTTP status ' + str(response[0]))
        self.response = response


def _fetch_one(parsed, deadline):
    from .phase1_budget_context import BudgetExhausted, physical_attempt, clamp_deadline
    deadline.end = clamp_deadline(deadline.end)
    entered = False
    try:
        with physical_attempt('public_page_http', max(0, deadline.end - time.monotonic())):
            entered = True
            deadline.remaining()
            response = _fetch_one_unbudgeted(parsed, deadline)
            status, headers, _, _ = response
            if status in {301, 302, 303, 307, 308} and headers.get('location'):
                _validate_url(urljoin(parsed.geturl(), headers['location']))
            elif status != 200:
                raise _HTTPReadFailure(response)
            return response
    except _HTTPReadFailure as error:
        # Preserve the HTTP receipt while the physical attempt records failure.
        return error.response
    except BudgetExhausted as error:
        if not entered:
            usage = getattr(error, 'usage', None)
            error.usage = {'timeout_stage': 'preflight', 'request_delivery_status': 'not_sent',
                           **(usage if isinstance(usage, dict) else {})}
        raise


def _fetch_one_unbudgeted(parsed, deadline):
    sock = _connect_pinned(parsed, deadline)
    connection = http.client.HTTPConnection(parsed.hostname, 443)
    connection.sock = sock
    try:
        path = quote(parsed.path, safe="/%:@!$&'()*+,;=-._~")
        query = quote(parsed.query, safe="%=&?/:@!$'()*+,;~-._")
        target = path + ("?" + query if query else "")
        sock.settimeout(deadline.remaining())
        connection.request("GET", target, headers={
            "User-Agent": "TravelMaterialReader/1.0", "Accept-Encoding": "identity",
            "Accept": "text/html,text/plain,application/xhtml+xml", "Connection": "close"})
        response = connection.getresponse()
        headers = {key.lower(): value for key, value in response.getheaders()}
        content_type = headers.get("content-type", "").split(";")[0].strip().lower()
        if (response.status != 200 or content_type not in {
                "text/html", "text/plain", "application/xhtml+xml"}
                or headers.get("content-encoding", "identity").lower() != "identity"):
            return response.status, headers, b"", False
        body, truncated = _read_body(response, sock, deadline)
        return response.status, headers, body, truncated
    finally:
        connection.close()


class _VisibleMaterial(HTMLParser):
    """Text-only adaptation of supply._VisibleText; no embedded JSON or assets."""

    HIDDEN = {"head", "script", "style", "noscript", "template", "svg", "iframe", "object", "canvas"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.parts, self.titles = [], [], []
        self.authentication = False
        self.shell_seen, self.shell_level = False, None
        self.substantive = []
        self.published_dates = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('property') == 'article:published_time':
            self.published_dates.append(attrs.get('content'))
        if tag == "input" and (attrs.get("type") or "").lower() == "password":
            self.authentication = True
        if tag in self.VOID:
            return
        if len(self.stack) >= MAX_HTML_DEPTH:
            raise _UnreadableContent("HTML nesting limit")
        style = (attrs.get("style") or "").lower().replace(" ", "")
        hidden = (tag in self.HIDDEN or "hidden" in attrs
                  or attrs.get("aria-hidden") == "true" or "display:none" in style
                  or "visibility:hidden" in style or bool(self.stack and self.stack[-1][1]))
        self.stack.append((tag, hidden))
        if (attrs.get("id") in {"app", "root", "__next", "__nuxt"} or tag == "app-root"):
            self.shell_seen = True
            self.shell_level = self.shell_level or len(self.stack)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                if self.shell_level is not None and len(self.stack) < self.shell_level:
                    self.shell_level = None
                break

    def handle_data(self, data):
        if any(tag == "title" for tag, _ in self.stack):
            self.titles.append(data)
        elif not self.stack or not self.stack[-1][1]:
            self.parts.append(data)
            if self.shell_level or any(tag in {"main", "article"} for tag, _ in self.stack):
                self.substantive.append(data)


class _UnreadableContent(Exception):
    """The response cannot be treated as useful visible text."""


def _social_excerpt(result, parser):
    """Retain a short audit quote, never the downloaded social page or images."""
    if not _reddit_host(urlsplit(result['url']).hostname or ''):
        return result
    published, freshness = None, 'unknown'
    dates = list(dict.fromkeys(parser.published_dates))
    if len(dates) == 1 and isinstance(dates[0], str):
        try:
            stamp = datetime.fromisoformat(dates[0].replace('Z', '+00:00'))
            if stamp.tzinfo is not None:
                published = dates[0]
                age = (datetime.fromisoformat(result['fetched_at']) - stamp).total_seconds()
                if age >= 0:
                    freshness = 'recent' if age <= 7 * 86400 else 'outdated'
        except (ValueError, OverflowError):
            pass
    # Even a short post must not become a retained whole-post copy.
    limit = min(MAX_SOCIAL_EXCERPT_CHARS, len(result['text']) // 2)
    excerpt = result['text'][:limit]
    return {**result, 'text': excerpt, 'content_complete': False,
            'truncated': result['truncated'] or len(result['text']) > len(excerpt),
            'content_scope': 'short excerpt only; omitted context may change interpretation',
            'verification': {'status': 'social_unverified', 'verified_fields': []},
            'factual_status': 'social_unverified',
            'source_provenance': {'source_type': 'reddit_post', 'platform': 'reddit',
                'source_ref': result['url'], 'excerpt': excerpt,
                'captured_at': result['fetched_at'], 'published_at': published,
                'experienced_at': None, 'freshness_status': freshness,
                'freshness_basis': 'published_at' if published else None,
                'freshness_window_days': 7,
                'limits': 'Leads/experience only; publication recency does not establish experience date.'},
            'message': '已读取公开帖短引文；仅作线索/体验对点，未经核实。'}


def _interpret_body(body, headers, result, truncated):
    content_type = headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in {"text/html", "text/plain", "application/xhtml+xml"}:
        return {**result, "reason": "unsupported_content", "message": "链接不是可读取的网页文本。"}
    if headers.get("content-encoding", "identity").lower() != "identity":
        return {**result, "reason": "unsupported_encoding", "message": "网页压缩格式暂不支持。"}
    metadata = Message()
    metadata["Content-Type"] = headers.get("content-type", "")
    try:
        decoded = body.decode(metadata.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        decoded = body.decode("utf-8", errors="replace")
    if ("\x00" in decoded or decoded.count("\ufffd") > max(3, len(decoded) // 20)
            or sum(ord(char) < 32 and char not in "\r\n\t" for char in decoded) > 3):
        raise _UnreadableContent("Response is not readable text")
    parser = _VisibleMaterial()
    if content_type == "text/plain":
        parser.parts.append(decoded)
    else:
        parser.feed(decoded)
        parser.close()
    text = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    title = re.sub(r"\s+", " ", " ".join(parser.titles)).strip()
    evidence = {"body_sha256": hashlib.sha256(body).hexdigest(),
                "truncated": truncated or len(text) > MAX_TEXT_CHARS, "bytes_read": len(body)}
    if any(pattern.search(title + "\n" + text) for pattern in _INJECTION_PATTERNS):
        return {**result, **evidence, "status": "blocked", "reason": "injection_alarm",
                "message": "网页包含可疑的模型指令，未把正文交给 Agent。"}
    empty_mount = parser.shell_seen and len(" ".join(parser.substantive).strip()) < 80
    if parser.authentication or _GATE.search(title + " " + text) or len(text) < 80 or empty_mount:
        return {**result, **evidence, "reason": "login_challenge_or_shell",
                "message": "网页需要登录、验证或脚本加载，或正文不足；可以补充截图。"}
    return _social_excerpt({**result, **evidence, "status": "read", "title": title[:240],
            "text": text[:MAX_TEXT_CHARS],
            "message": ("已读取部分网页文本（达到读取上限）；网页说法未经核实。"
                        if evidence['truncated'] else "已读取网页文本；网页说法未经核实。")}, parser)


def _read_validated(parsed, deadline, result):
    for hop in range(MAX_REDIRECTS + 1):
        # Only URLs accepted by _validate_url enter this server-owned trace.
        # Keep it on failures too; a dead redirect destination invalidates its
        # attempted public entry points, not just the last URL in the chain.
        result.update(url=parsed.geturl(), http_status=None,
                      redirect_chain=[*result.get('redirect_chain', []), parsed.geturl()])
        status, headers, body, truncated = _fetch_one(parsed, deadline)
        result['http_status'] = status
        deadline.remaining()
        if status in {301, 302, 303, 307, 308}:
            if hop == MAX_REDIRECTS or not headers.get("location"):
                return {**result, "reason": "redirect_limit", "message": "链接跳转过多或缺少跳转地址。"}
            destination = _validate_url(urljoin(parsed.geturl(), headers["location"]))
            if _reddit_host(parsed.hostname) and not _reddit_host(destination.hostname):
                raise ValueError('Reddit public post redirects cannot leave Reddit')
            parsed = destination
            continue
        if status != 200:
            return {**result, "reason": "http_unavailable", "message": "网页暂不可读，可能需要登录或已失效。"}
        return _interpret_body(body, headers, {**result, "url": parsed.geturl()}, truncated)
    return result


def read_public_link(url, *, deadline=None) -> dict:
    """Read a single public HTTPS page within eight seconds, or give an honest receipt."""
    result = {"status": "unavailable", "title": "", "text": "", "url": "",
              "method": "direct_http", "http_status": None,
              "message": "网页暂时无法读取，可以补充截图。",
              "fetched_at": datetime.now(timezone.utc).isoformat(),
              "untrusted_external_content": True, "verified_fields": []}
    try:
        parsed = _validate_url(url)
    except (ValueError, UnicodeError):
        return {**result, "status": "blocked", "reason": "url_policy",
                "message": "仅支持无凭证的公开 HTTPS 链接；内网、登录或订单链接不会读取。"}
    result = {**result, "url": parsed.geturl(), "requested_url": parsed.geturl(),
              "redirect_chain": []}
    parent_deadline = deadline
    if parent_deadline is not None and time.monotonic() >= parent_deadline:
        return {**result, 'reason': 'timeout', 'message': '本轮读取预算已用完，未发起网页读取。',
                'timeout_stage': 'preflight', 'request_delivery_status': 'not_sent',
                'request_delivery_scope': 'current_http_attempt'}
    if not _WORKERS.acquire(blocking=False):
        return {**result, "reason": "reader_busy", "message": "链接读取繁忙，请稍后重试。"}
    deadline, output = _ReadDeadline(), queue.Queue(maxsize=1)
    if parent_deadline is not None:
        deadline.end = min(deadline.end, parent_deadline)

    def worker():
        try:
            output.put(_read_validated(parsed, deadline, result))
        except BudgetExhausted as error:
            usage = getattr(error, 'usage', None)
            facts = {key: value for key, value in (usage.items() if isinstance(usage, dict) else [])
                     if key in {'timeout_stage', 'request_delivery_status', 'provider_request_id',
                                'provider_request_id_present', 'usage_status', 'usage_complete'}}
            output.put({**result, **facts, 'reason': 'budget_exhausted',
                        'error_type': type(error).__name__, 'error_message': str(error),
                        'error_code': getattr(error, 'code', None),
                        'request_delivery_scope': 'current_http_attempt'})
        except ValueError:
            output.put({**result, "status": "blocked", "reason": "destination_policy",
                        "message": "链接目的地或跳转不符合公开无凭证读取规则。"})
        except _UnreadableContent:
            output.put({**result, "reason": "unreadable_body"})
        except (OSError, TimeoutError, http.client.HTTPException, UnicodeError):
            output.put({**result, "reason": "fetch_failed"})
        finally:
            deadline.cancel()
            _WORKERS.release()

    from .phase1_budget_context import BudgetExhausted, context_target
    threading.Thread(target=context_target(worker), name="public-material-reader", daemon=True).start()
    try:
        return output.get(timeout=max(0, deadline.end - time.monotonic()))
    except queue.Empty:
        deadline.cancel()
        return {**result, "reason": "timeout", "message": "链接读取超时，可以补充截图。"}
