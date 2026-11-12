"""Loopback HTTP adapter for the existing single-conversation agent."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import fcntl
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import urlsplit, unquote

_DEFAULT_MEDIA = object()
MAX_BODY = 256 * 1024
TRIP_ID = re.compile(r"[0-9a-f]{32}")
ROUTE = re.compile(r"/api/agent/trips/([0-9a-f]{32})(?:/(message|revise|handbook|document|pdf))?")


def _now():
    return datetime.now(timezone.utc).isoformat()


class APIError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def _text(value, name, limit=30000, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise APIError(422, "invalid_input", f"{name} must be text of at most {limit} characters")
    return value.strip()


def validate_intake(payload):
    if not isinstance(payload, dict):
        raise APIError(422, "invalid_input", "request must be a JSON object")
    allowed = {"brief", "evidence_text", "links", "documents", "images", "destination",
               "start_date", "end_date", "travelers", "composition", "pace", "preferences"}
    if set(payload) - allowed:
        raise APIError(422, "unsupported_fields", "unsupported fields: " + ", ".join(sorted(set(payload) - allowed)))
    result = {}
    for key in ("documents", "images"):
        value = payload.get(key, [])
        if not isinstance(value, list) or value:
            raise APIError(422, "attachments_unsupported", "documents and images are not supported; provide selected text or public links")
    for key in ("brief", "evidence_text", "destination", "start_date", "end_date", "pace"):
        if key in payload:
            result[key] = _text(payload[key], key)
    if not result.get("brief") and not result.get("destination"):
        raise APIError(422, "invalid_input", "brief or destination is required")
    for key in ("start_date", "end_date"):
        if result.get(key):
            try:
                date.fromisoformat(result[key])
            except ValueError as exc:
                raise APIError(422, "invalid_input", key + " must be YYYY-MM-DD") from exc
    if result.get("start_date") and result.get("end_date") and result["start_date"] > result["end_date"]:
        raise APIError(422, "invalid_input", "end_date must not precede start_date")
    if "travelers" in payload:
        value = payload["travelers"]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
            raise APIError(422, "invalid_input", "travelers must be an integer from 1 to 100")
        result["travelers"] = value
    for key in ("composition", "preferences"):
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, str):
            result[key] = _text(value, key)
        elif isinstance(value, list) and len(value) <= 50:
            result[key] = [_text(item, key, 2000) for item in value]
        else:
            raise APIError(422, "invalid_input", key + " must be text or a list of text")
    links = payload.get("links", [])
    if not isinstance(links, list) or len(links) > 30:
        raise APIError(422, "invalid_input", "links must be a list of at most 30 public URLs")
    result["links"] = []
    for item in links:
        link = _text(item, "link", 4096, True)
        parsed = urlsplit(link)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise APIError(422, "invalid_input", "links must be HTTP(S) URLs without credentials")
        result["links"].append(link)
    return result


def validate_revision(payload):
    fields_allowed = {"message", "instruction", "text", "feedback", "expected_revision",
                      "request_id", "expected_hash", "locked_item_ids", "materials",
                      "evidence_text", "links", "documents", "images"}
    if not isinstance(payload, dict) or set(payload) - fields_allowed:
        raise APIError(422, "invalid_input", "provide revision text and supported revision fields")
    if payload.get("locked_item_ids", []) != []:
        raise APIError(422, "locks_unsupported", "locked_item_ids are not supported by the single-session service")
    if payload.get("materials") not in (None, {}, []):
        raise APIError(422, "materials_unsupported", "revision materials are not supported; include selected text in feedback")
    for key, empty in (("evidence_text", ""), ("links", []), ("documents", []), ("images", [])):
        if key in payload and payload[key] != empty:
            raise APIError(422, "materials_unsupported", "revision materials are not supported; include selected text in feedback")
    fields = [key for key in ("message", "instruction", "text", "feedback") if key in payload]
    if len(fields) != 1:
        raise APIError(422, "invalid_input", "provide exactly one revision text field")
    for key in ("request_id", "expected_hash"):
        if key in payload:
            _text(payload[key], key, 128, required=True)
    if "expected_revision" in payload and (type(payload["expected_revision"]) is not int or payload["expected_revision"] < 1):
        raise APIError(422, "invalid_input", "expected_revision must be a positive integer")
    return _text(payload[fields[0]], fields[0], required=True)


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(6))
    with open(temporary, "x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _agent(directory):
    from .single_session_agent import SingleSessionAgent
    return SingleSessionAgent(directory, supply_cache_path=directory.parent.parent / 'supply-cache.sqlite3')


class SessionService:
    """Own one local data directory and serialize each conversation's writes."""
    def __init__(self, directory, agent_factory=None, workers=4, pdf_renderer=None,
                 media_provider=_DEFAULT_MEDIA):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lockfile = open(self.directory / ".service.lock", "a+")
        try:
            fcntl.flock(self._lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lockfile.close()
            raise RuntimeError("another service already owns this data directory")
        from .single_session_pdf_runner import render_service_pdf
        self.pdf_renderer = pdf_renderer or render_service_pdf
        self.agent_factory = agent_factory or _agent
        if media_provider is _DEFAULT_MEDIA:
            from .single_session_runtime_media import RuntimeMediaProvider
            media_provider = RuntimeMediaProvider() if agent_factory is None else None
        self.media_provider = media_provider
        self.guard = threading.RLock()
        self.records = {}
        from .single_session_profiles import ProfileStore
        self.profiles = ProfileStore(self.directory, self.guard)
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="single-session")
        self._restore()

    def _restore(self):
        for path in self.directory.glob("*/api-record.json"):
            trip_id = path.parent.name
            if not TRIP_ID.fullmatch(trip_id) or path.parent.is_symlink():
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("id") != trip_id:
                raise ValueError("invalid saved trip identity")
            self.records[trip_id] = record
            if record["status"] in {"processing", "queued", "running"}:
                record.update(status="failed", error={"code": "interrupted", "message": "Service stopped during a turn; inspect local receipts before any further request. No request was replayed."})
                record["jobs"][-1].update(status="interrupted", finished_at=_now())
                # A document can have been committed just before interruption.
                self._capture_existing_document(record)
                self._save(record)

    def _capture_existing_document(self, record):
        from .single_session_document import DocumentStore
        directory = self.directory / record["id"] / "session" / "documents"
        if (directory / "versions.json").is_file():
            current = DocumentStore(directory).current()
            if current and not record.get("document"):
                document = self._document(record, current)
                if document.get("pdf_status") == "ready":
                    record["document"] = document

    def _save(self, record):
        record["updated_at"] = _now()
        _atomic_json(self.directory / record["id"] / "api-record.json", record)

    def _document(self, record, result):
        document = {key: result[key] for key in ("version", "hash", "diff", "receipt", "price_checks", "presentation_receipt") if key in result}
        document["handbook_url"] = f"/api/agent/trips/{record['id']}/handbook?read={record['read_capability']}"
        from .single_session_pdf import pdf_receipt
        directory = self.directory / record['id'] / 'session' / 'documents'
        from .single_session_presentation_delivery import select_presentation
        html_path, presentation = select_presentation(directory, result["version"])
        if presentation is not None:
            document["presentation_receipt"] = presentation
        try:
            document['pdf'] = pdf_receipt(html_path, html_path.with_suffix('.pdf'))
            document['pdf_url'] = (f"/api/agent/trips/{record['id']}/pdf?"
                                   f"version={result['version']}&read={record['read_capability']}")
            document['pdf_status'] = 'ready'
        except Exception:
            document['pdf_status'] = 'unavailable'
            document['pdf_url'] = None
        return document

    def _public(self, record):
        document = deepcopy(record.get("document"))
        if document:
            document = self._document(record, document)
        return {**({"profile_application": deepcopy(record["profile_application"])} if "profile_application" in record else {}),
                "id": record["id"], "status": record["status"], "brief": record["intake"].get("brief", record["intake"].get("destination", "")),
                "revision": document["version"] if document else 0,
                "created_at": record["created_at"], "updated_at": record["updated_at"], "plan": None,
                "error": record.get("error"), "document": document,
                "handbook_url": document.get("handbook_url") if document else None,
                "pdf_url": document.get("pdf_url") if document else None,
                "receipt": document.get("receipt") if document else None,
                "presentation_receipt": document.get("presentation_receipt") if document else None,
                "diff": document.get("diff") if document else None,
                "job": {key: value for key, value in record["jobs"][-1].items() if key not in {"instruction", "pdf_error"}},
                "capabilities": {"single_session": True, "revision": True, "handbook_html": True, "handbook_pdf": True,
                                 "structured_plan": False, "attachments": False}}

    def create(self, payload):
        intake, profile_application = self.profiles.apply(payload)
        fingerprint = _fingerprint(intake)
        trip_id, token = secrets.token_hex(16), secrets.token_urlsafe(32)
        record = {"id": trip_id, "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                  "read_capability": secrets.token_urlsafe(32), "intake": intake,
                  "created_at": _now(), "status": "running", "document": None,
                  "jobs": [{"id": secrets.token_hex(16), "kind": "generate", "status": "queued", "started_at": _now()}]}
        if profile_application is not None:
            record["profile_application"] = profile_application
        with self.guard:
            if any(_fingerprint({key: value for key, value in saved["intake"].items()
                                 if key != "case_id"}) == fingerprint
                   for saved in self.records.values()):
                raise APIError(409, "duplicate_request", "Identical intake already submitted; restore the saved trip instead of rerunning.")
            intake["case_id"] = trip_id
            (self.directory / trip_id).mkdir(mode=0o700)
            self.records[trip_id] = record
            self._save(record)
            self.executor.submit(self._run, trip_id, None)
            return {"trip": self._public(record), "token": token}

    def _authorize(self, trip_id, token):
        record = self.records.get(trip_id)
        digest = hashlib.sha256(token.encode()).hexdigest()
        if record is None or not hmac.compare_digest(record["token_hash"], digest):
            raise APIError(404, "not_found", "trip not found")
        return record

    def _progress_json(self, path):
        """Read optional persisted state without constructing an agent/store."""
        try:
            if not path.resolve().is_relative_to(self.directory):
                return {}
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _progress(self, record):
        directory = self.directory / record["id"]
        saved = self._progress_json(directory / "api-record.json")
        session = self._progress_json(directory / "session" / "conversation.json")
        ledger = self._progress_json(directory / "session" / "documents" / "ledger.json")
        manifest = self._progress_json(directory / "session" / "documents" / "versions.json")
        turns = [row for row in session.get("turns", []) if isinstance(row, dict)]
        activities = [row for row in session.get("tools", []) if isinstance(row, dict)]
        counts = dict.fromkeys(("search", "read_page", "record_evidence", "save_handbook", "edit_handbook"), 0)
        for activity in activities:
            if activity.get("name") in counts:
                counts[activity["name"]] += 1
        jobs = saved.get("jobs") or []
        job = jobs[-1] if jobs else {}
        version = manifest.get("current") or (saved.get("document") or {}).get("version", 0)
        active = bool(turns or activities or version or job.get("status") in {"failed", "interrupted"})
        state = None
        if active:
            state = {"completed": "completed", "failed": "failed", "interrupted": "failed"}.get(job.get("status"))
            if state is None and job.get("status") in {"queued", "running"}:
                state = "revising" if job.get("kind") == "revise" else "generating"
        timestamps = []
        for row in turns + activities + (jobs if active else []):
            for key in ("started_at", "finished_at"):
                try:
                    stamp = datetime.fromisoformat(row[key])
                    if stamp.tzinfo is not None:
                        timestamps.append(stamp.astimezone(timezone.utc))
                except (KeyError, TypeError, ValueError):
                    pass
        return {"status": state,
                "completed_turns": sum(row.get("status") == "completed" for row in turns),
                "tool_counts": counts, "evidence_count": len(ledger.get("evidence") or {}),
                "last_activity_at": max(timestamps).isoformat() if timestamps else None,
                "document_version": version}

    def get(self, trip_id, token):
        with self.guard:
            record = self._authorize(trip_id, token)
            trip = self._public(record)
            trip["progress"] = self._progress(record)
            return {"trip": trip}

    def revise(self, trip_id, token, payload):
        message = validate_revision(payload)
        with self.guard:
            record = self._authorize(trip_id, token)
            request_id = payload.get("request_id")
            fingerprint = _fingerprint({"message": message, "expected_revision": payload.get("expected_revision"), "expected_hash": payload.get("expected_hash")})
            for job in record["jobs"]:
                if request_id and job.get("request_id") == request_id:
                    if job.get("request_fingerprint") != fingerprint:
                        raise APIError(409, "idempotency_conflict", "request_id already belongs to different input")
                    return {"trip": self._public(record), "kind": "revision"}
                if job.get("request_fingerprint") == fingerprint:
                    raise APIError(409, "duplicate_request", "Identical revision already submitted; read saved status instead of rerunning.")
            current = record.get("document") or {}
            if (("expected_revision" in payload and payload["expected_revision"] != current.get("version"))
                    or ("expected_hash" in payload and payload["expected_hash"] != current.get("hash"))):
                raise APIError(409, "revision_conflict", "Document changed; read the latest document before revising")
            if record["status"] != "completed":
                raise APIError(409, "turn_unavailable", "trip is busy or its prior turn did not complete; no request sent")
            expected = payload.get("expected_revision")
            if expected is not None and expected != record["document"]["version"]:
                raise APIError(409, "stale_revision", "expected_revision does not match the current document")
            record.update(status="running", error=None)
            record["jobs"].append({"id": secrets.token_hex(16), "kind": "revise", "status": "queued", "started_at": _now()})
            # Persist user intent before a worker can send anything.
            record["jobs"][-1].update(instruction=message, request_id=request_id, request_fingerprint=fingerprint)
            self._save(record)
            self.executor.submit(self._run, trip_id, message)
            return {"trip": self._public(record), "kind": "revision"}

    def _run(self, trip_id, message):
        with self.guard:
            record = self.records[trip_id]
            record["jobs"][-1]["status"] = "running"
            self._save(record)
        try:
            agent = self.agent_factory(self.directory / trip_id / "session")
            if message is None:
                if agent.session["turns"]:
                    raise RuntimeError("initial conversation already started")
                agent.preflight(record["intake"])
                result = asyncio.run(agent.run_turn(json.dumps(record["intake"], ensure_ascii=False), intake=record["intake"]))
            else:
                result = asyncio.run(agent.run_turn(message))
            if not result.get("document_saved") or not agent.session["turns"] or agent.session["turns"][-1]["status"] != "completed":
                raise RuntimeError("turn did not confirm a saved document")
            current = agent.documents.current()
            if not current or current["hash"] != result["hash"]:
                raise RuntimeError("saved document receipt mismatch")
            from .single_session_completeness import validate_completion
            validate_completion(current['markdown'], record['intake'])
            from .single_session_pdf import pdf_receipt
            from .single_session_presentation_delivery import present_saved
            html_path, presentation = present_saved(
                Path(current['html_path']).parent, current, record['intake'],
                media_provider=self.media_provider)
            current = {**current, 'presentation_receipt': presentation}
            try:
                self.pdf_renderer(html_path, html_path.with_suffix('.pdf'))
                pdf_receipt(html_path, html_path.with_suffix('.pdf'))
            except Exception as exc:
                pdf_error = {
                    'at': _now(), 'error_type': type(exc).__name__, 'detail': str(exc),
                    'version': current['version'], 'html_path': str(html_path),
                    'recovery': 'Render this immutable HTML with tools/render_handbook_pdf.py; no model replay.'}
                with self.guard:
                    _atomic_json(self.directory / trip_id / 'pdf-error.json', pdf_error)
                    record['jobs'][-1]['pdf_error'] = pdf_error

            with self.guard:
                record.update(status="completed", error=None, document=self._document(record, current))
                record["jobs"][-1].update(status="completed", finished_at=_now(), version=current["version"])
                self._save(record)
        except BaseException as exc:
            from .single_session_pdf import PDFError
            with self.guard:
                if isinstance(exc, PDFError):
                    _atomic_json(self.directory / trip_id / 'pdf-error.json', {
                        'at': _now(), 'error_type': type(exc).__name__, 'detail': str(exc),
                        'version': current['version'], 'html_path': str(html_path),
                        'recovery': 'Render this immutable HTML with tools/render_handbook_pdf.py; no model replay.'})
                record.update(status="failed", error={"code": "pdf_failed" if isinstance(exc, PDFError) else "turn_failed", "message": "Turn or PDF output failed; inspect local receipts. No automatic retry was made.", "type": type(exc).__name__})
                record["jobs"][-1].update(status="failed", finished_at=_now(), error_type=type(exc).__name__)
                if not isinstance(exc, PDFError):
                    self._capture_existing_document(record)
                self._save(record)

    def handbook(self, trip_id, token="", read_capability=""):
        with self.guard:
            record = self.records.get(trip_id)
            if not record:
                raise APIError(404, "not_found", "trip not found")
            if not read_capability or not hmac.compare_digest(record["read_capability"], read_capability):
                self._authorize(trip_id, token)
            if not record.get("document"):
                raise APIError(404, "not_ready", "handbook is not yet available")
            version = record["document"]["version"]
            directory = (self.directory / trip_id / "session" / "documents").resolve()
            from .single_session_presentation_delivery import select_presentation
            path, _ = select_presentation(directory, version)
            if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(self.directory):
                raise APIError(404, "not_found", "handbook not found")
            return path.read_bytes()

    def pdf(self, trip_id, token="", read_capability="", version=None):
        from .single_session_pdf import pdf_receipt
        with self.guard:
            record = self.records.get(trip_id)
            if not record:
                raise APIError(404, "not_found", "trip not found")
            if not read_capability or not hmac.compare_digest(record['read_capability'], read_capability):
                self._authorize(trip_id, token)
            current = record.get('document') or {}
            version = current.get('version') if version is None else version
            if type(version) is not int or version < 1 or version > current.get('version', 0):
                raise APIError(404, "not_ready", "PDF version is not available")
            directory = self.directory / trip_id / 'session' / 'documents'
            from .single_session_presentation_delivery import select_presentation
            html_path, _ = select_presentation(directory, version)
            target = html_path.with_suffix('.pdf')
            if not target.resolve().is_relative_to(self.directory / trip_id):
                raise APIError(404, "not_found", "PDF not found")
            try:
                pdf_receipt(target.with_suffix('.html'), target)
                return target.read_bytes()
            except (OSError, ValueError, RuntimeError):
                raise APIError(404, "pdf_unavailable", "Verified PDF is not available")

    def close(self):
        self.executor.shutdown(wait=True)
        fcntl.flock(self._lockfile, fcntl.LOCK_UN)
        self._lockfile.close()


class SessionHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service, static_root=None):
        if address[0] != "127.0.0.1":
            raise ValueError("single-session service must bind to 127.0.0.1")
        self.service = service
        self.static_root = Path(static_root).resolve() if static_root else None
        super().__init__(address, SessionHandler)


class SessionHandler(BaseHTTPRequestHandler):
    server_version = "SingleSession/1"

    def log_message(self, *args):
        pass  # Capability URLs and user inputs must not appear in access logs.

    def _origin(self):
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "")
        if host not in hosts:
            raise APIError(403, "invalid_host", "loopback Host required")
        origin = self.headers.get("Origin")
        if origin is not None and origin != "http://" + host:
            raise APIError(403, "invalid_origin", "same-origin requests required")

    def _reply(self, status, value, content_type="application/json; charset=utf-8"):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'self'")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
            raise APIError(415, "json_required", "application/json with Content-Length required")
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError as exc:
            raise APIError(400, "invalid_length", "invalid Content-Length") from exc
        if not 0 <= length <= MAX_BODY:
            raise APIError(413, "body_too_large", f"request body must be at most {MAX_BODY} bytes")
        self.connection.settimeout(10)
        try:
            data = self.rfile.read(length)
            if len(data) != length:
                raise ValueError("truncated body")
            return json.loads(data)
        except (ValueError, UnicodeError, TimeoutError) as exc:
            raise APIError(400, "invalid_json", "valid UTF-8 JSON required") from exc

    def _token(self):
        authorization = self.headers.get("Authorization", "")
        return authorization[7:] if authorization.startswith("Bearer ") else ""

    def _dispatch(self):
        self._origin()
        parsed = urlsplit(self.path)
        path = parsed.path
        service = self.server.service
        if self.command == "POST" and path == "/api/agent/trips":
            self._reply(202, service.create(self._body()))
            return
        if self.command == "POST" and path == "/api/agent/profiles":
            self._reply(201, service.profiles.create(self._body()))
            return
        profile_match = re.fullmatch(r"/api/agent/profiles/([0-9a-f]{32})", path)
        if profile_match:
            profile_id = profile_match[1]
            if self.command == "GET":
                self._reply(200, service.profiles.get(profile_id))
                return
            if self.command == "PATCH":
                self._reply(200, service.profiles.update(profile_id, self._body()))
                return
            if self.command == "DELETE":
                self._reply(200, service.profiles.delete(profile_id))
                return
        match = ROUTE.fullmatch(path)
        if match:
            trip_id, action = match.groups()
            if self.command == "GET" and action is None:
                self._reply(200, service.get(trip_id, self._token()))
                return
            if self.command == "POST" and action in {"message", "revise"}:
                self._reply(202, service.revise(trip_id, self._token(), self._body()))
                return
            if self.command == "GET" and action == "pdf":
                from urllib.parse import parse_qs
                query = parse_qs(parsed.query)
                try:
                    version = int(query['version'][0]) if 'version' in query else None
                except ValueError:
                    raise APIError(422, "invalid_version", "version must be an integer")
                self._reply(200, service.pdf(trip_id, self._token(), query.get('read', [''])[0], version),
                            "application/pdf")
                return
            if self.command == "GET" and action in {"handbook", "document"}:
                from urllib.parse import parse_qs
                read_capability = parse_qs(parsed.query).get("read", [""])[0]
                self._reply(200, service.handbook(trip_id, self._token(), read_capability), "text/html; charset=utf-8")
                return
        if self.command == "GET" and path == "/api/agent/capabilities":
            self._reply(200, {"planner_available": bool(os.environ.get("DEEPSEEK_API_KEY")), "single_session": True, "revision": True, "attachments": False, "structured_plan": False})
            return
        if self.command == "GET" and path == "/api/agent/autopilot/capabilities":
            self._reply(200, {"enabled": False})
            return
        if self.command == "GET" and path == "/api/maps-config":
            self._reply(200, {"configured": False})
            return
        if self.command == "GET" and not path.startswith("/api/") and self.server.static_root:
            self._static(path)
            return
        raise APIError(404, "unsupported_endpoint", "endpoint is not supported by the single-session service")

    def _static(self, path):
        root = self.server.static_root
        decoded = unquote(path)
        if "\x00" in decoded or any(part.startswith(".") for part in Path(decoded).parts):
            raise APIError(404, "not_found", "file not found")
        target = (root / decoded.lstrip("/")).resolve()
        if not target.is_relative_to(root):
            raise APIError(404, "not_found", "file not found")
        if target.is_dir():
            target = target / "studio.html"
        if not target.is_file():
            raise APIError(404, "not_found", "file not found")
        self._reply(200, target.read_bytes(), mimetypes.guess_type(target.name)[0] or "application/octet-stream")

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def _handle(self):
        try:
            self._dispatch()
        except APIError as exc:
            self._reply(exc.status, {"error": {"code": exc.code, "message_zh": str(exc)}})
        except Exception:
            self._reply(500, {"error": {"message_zh": "Internal service error; inspect local receipts", "code": "internal_error"}})
