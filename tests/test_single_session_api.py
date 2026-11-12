"""Offline HTTP contracts: stub execution with the real document store."""
import asyncio
from datetime import date, timedelta
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from travel_agent.single_session_api import APIError, MAX_BODY, SessionHTTPServer, SessionService
from travel_agent.single_session_document import DocumentStore


def fake_pdf(source, target):
    """Unit-only artifact; no rendering/embedded-image evidence is claimed."""
    from travel_agent.single_session_pdf import digest
    data = b'%PDF-unit-stub'
    target.write_bytes(data)
    receipt = dict(status='ready', html_images=0, embedded_images=0, pages=1,
                   html_sha256=digest(source.read_bytes()), pdf_sha256=digest(data))
    target.with_suffix('.pdf.json').write_text(json.dumps(receipt))
    return receipt


class OfflineAgent:
    entered = release = None
    failure = False

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.documents = DocumentStore(self.directory / "documents")
        self.path = self.directory / "offline-session.json"
        self.session = json.loads(self.path.read_text()) if self.path.exists() else {"turns": []}

    def preflight(self, intake):
        assert len(intake["case_id"]) == 32
        return {"product_calls": 0}

    async def run_turn(self, text, *, intake=None):
        if self.entered:
            self.entered.set()
            await asyncio.to_thread(self.release.wait, 5)
        if self.failure:
            raise RuntimeError("SECRET /private/raw/conversation.json")
        if intake is not None:
            assert not self.session["turns"]
            source = "# Offline test\n\n原来的行程"
            if intake.get('start_date') and intake.get('end_date'):
                start, end = date.fromisoformat(intake['start_date']), date.fromisoformat(intake['end_date'])
                days = [dict(id=f'day-{i+1}', title=f'Day {i+1}', summary='Offline rest day',
                             date=(start + timedelta(days=i)).isoformat(),
                             timeline=[dict(kind='activity', action='Rest')])
                        for i in range((end-start).days + 1)]
                source += '\n```destination-detail-v1\n' + json.dumps(dict(version=1, days=days)) + '\n```'
            result = self.documents.save(source)
        else:
            assert self.session["turns"]
            result = self.documents.edit(self.documents.current()["hash"],
                [{"old": "原来的行程", "new": "修改后的行程"}],
                {"changes": "已修改", "judgment": "放慢节奏", "alternatives": "原计划", "next_action": "阅读手册"})
        self.session["turns"].append({"status": "completed"})
        self.path.write_text(json.dumps(self.session))
        return {**result, "document_saved": True}


class SessionAPITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        OfflineAgent.entered = OfflineAgent.release = None
        OfflineAgent.failure = False
        self.service = SessionService(self.root / "data", OfflineAgent, pdf_renderer=fake_pdf)
        self.static = self.root / "static"
        self.static.mkdir()
        (self.static / "studio.html").write_text("<html>studio</html>")
        self.server = SessionHTTPServer(("127.0.0.1", 0), self.service, self.static)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        if OfflineAgent.release:
            OfflineAgent.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.service.close()
        self.temporary.cleanup()

    def request(self, method, path, payload=None, token=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        body = json.dumps(payload) if payload is not None else None
        values = {"Content-Type": "application/json"}
        if token:
            values["Authorization"] = "Bearer " + token
        values.update(headers or {})
        connection.request(method, path, body=body, headers=values)
        response = connection.getresponse()
        data, content_type = response.read(), response.getheader("Content-Type")
        connection.close()
        return response.status, json.loads(data) if "application/json" in content_type else data

    def completed(self, trip_id, token):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            trip = self.service.get(trip_id, token)["trip"]
            if trip["status"] != "running":
                return trip
            time.sleep(.01)
        self.fail("offline worker did not terminate")

    def create(self):
        status, created = self.request("POST", "/api/agent/trips", {"brief": "任意城市", "evidence_text": "", "links": [], "documents": [], "images": []})
        self.assertEqual(status, 202)
        return created["trip"]["id"], created["token"]

    def test_http_generation_revision_read_only_link(self):
        trip_id, token = self.create()
        trip = self.completed(trip_id, token)
        self.assertEqual(trip["status"], "completed")
        self.assertEqual(trip["revision"], 1)
        self.assertIsNone(trip["plan"])
        self.assertNotIn(token, trip["handbook_url"])
        self.assertEqual(self.request("GET", f"/api/agent/trips/{trip_id}")[0], 404)
        self.assertEqual(self.request("GET", f"/api/agent/trips/{trip_id}", token=token)[0], 200)
        status, page = self.request("GET", trip["handbook_url"])
        self.assertEqual(status, 200)
        self.assertIn("原来的行程".encode(), page)
        status, revision = self.request("POST", f"/api/agent/trips/{trip_id}/message", {"feedback": "放慢", "expected_revision": 1, "locked_item_ids": [], "evidence_text": "", "links": [], "documents": [], "images": []}, token=token)
        self.assertEqual(status, 202)
        self.assertEqual(revision["kind"], "revision")
        revised = self.completed(trip_id, token)
        self.assertEqual(revised["revision"], 2)
        self.assertIn("修改后的行程", revised["diff"])
        self.assertEqual(revised["receipt"]["changes"], "已修改")
        self.assertNotIn("html_path", json.dumps(revised))
        self.assertNotIn("conversation", json.dumps(revised))
        read_token = trip["handbook_url"].split("read=")[1]
        self.assertEqual(self.request("POST", f"/api/agent/trips/{trip_id}/message", {"feedback": "修改"}, token=read_token)[0], 404)
        self.assertEqual(self.request("POST", f"/api/agent/trips/{trip_id}/message", {"feedback": "修改", "expected_revision": 1}, token=token)[0], 409)

    def test_busy_and_directory_lock(self):
        OfflineAgent.entered, OfflineAgent.release = threading.Event(), threading.Event()
        created = self.service.create({"destination": "某城市", "start_date": "2026-12-01", "end_date": "2026-12-04", "travelers": 3, "composition": ["两位成人", "一位儿童"], "pace": "慢", "preferences": ["步行"]})
        trip_id, token = created["trip"]["id"], created["token"]
        self.assertEqual(created["trip"]["status"], "running")
        self.assertTrue(OfflineAgent.entered.wait(3))
        self.assertEqual(self.request("POST", f"/api/agent/trips/{trip_id}/revise", {"text": "change"}, token=token)[0], 409)
        with self.assertRaisesRegex(RuntimeError, "already owns"):
            SessionService(self.service.directory, OfflineAgent)
        OfflineAgent.release.set()
        self.assertEqual(self.completed(trip_id, token)["status"], "completed")

    def test_restart_completed_and_interrupted_never_replays(self):
        trip_id, token = self.create()
        self.assertEqual(self.completed(trip_id, token)["status"], "completed")
        for interrupted in (False, True):
            directory = self.service.directory
            self.service.close()
            if interrupted:
                path = directory / trip_id / "api-record.json"
                record = json.loads(path.read_text())
                record["status"] = "running"
                record["jobs"][-1]["status"] = "running"
                path.write_text(json.dumps(record))
            self.service = SessionService(directory, lambda _: self.fail("restore called model"))
            self.server.service = self.service
            trip = self.service.get(trip_id, token)["trip"]
            self.assertEqual(trip["status"], "failed" if interrupted else "completed")
            self.assertEqual(trip["document"]["version"], 1)
            self.assertIn(b"<!doctype html>", self.service.handbook(trip_id, token))
            if interrupted:
                with self.assertRaises(APIError):
                    self.service.revise(trip_id, token, {"message": "retry"})

    def test_create_dedup_excludes_random_case_id(self):
        trip_id, token = self.create()
        self.completed(trip_id, token)
        self.assertEqual(self.service.records[trip_id]["intake"]["case_id"], trip_id)
        status, error = self.request("POST", "/api/agent/trips", {
            "brief": "任意城市", "evidence_text": "", "links": [], "documents": [], "images": []})
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "duplicate_request")
        self.assertEqual(len(self.service.records), 1)

    def test_revision_request_id_hash_and_replay(self):
        trip_id, token = self.create()
        trip = self.completed(trip_id, token)
        route = f"/api/agent/trips/{trip_id}/message"
        payload = {"feedback": "放慢", "expected_revision": 1,
                   "expected_hash": trip["document"]["hash"], "request_id": "edit-1"}
        status, error = self.request("POST", route, {**payload, "expected_hash": "wrong"}, token)
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "revision_conflict")
        status, accepted = self.request("POST", route, payload, token)
        self.assertEqual(status, 202)
        self.assertEqual(accepted["kind"], "revision")
        self.assertEqual(self.completed(trip_id, token)["revision"], 2)
        status, replay = self.request("POST", route, payload, token)
        self.assertEqual(status, 202)
        self.assertEqual(replay["kind"], "revision")
        self.assertEqual(replay["trip"]["revision"], 2)
        self.assertEqual(len(self.service.records[trip_id]["jobs"]), 2)
        status, error = self.request("POST", route, {**payload, "feedback": "换方案"}, token)
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "idempotency_conflict")

    def test_invalid_inputs_and_cross_origin_make_no_records(self):
        invalid = [{"brief": "city", "images": ["secret.png"]}, {"brief": "city", "documents": ["private.pdf"]},
                   {"brief": "city", "travelers": True}, {"brief": "city", "links": ["file:///etc/passwd"]},
                   {"brief": "city", "unexpected": 1}, {"destination": "city", "start_date": "bad"}]
        for value in invalid:
            self.assertEqual(self.request("POST", "/api/agent/trips", value)[0], 422)
        for header in ({"Origin": "https://evil.example"}, {"Host": "evil.example"}):
            self.assertEqual(self.request("POST", "/api/agent/trips", {"brief": "city"}, headers=header)[0], 403)
        self.assertEqual(self.request("POST", "/api/agent/trips", {"brief": "city"}, headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/agent/trips", {"brief": "x" * MAX_BODY})[0], 413)
        self.assertFalse(self.service.records)

    def test_unsupported_revision_fields_rejected(self):
        trip_id, token = self.create()
        self.completed(trip_id, token)
        for payload in ({"feedback": "x", "message": "x"}, {"feedback": "x", "locked_item_ids": ["item"]}, {"feedback": "x", "materials": {"text": "secret"}}):
            status, response = self.request("POST", f"/api/agent/trips/{trip_id}/message", payload, token=token)
            self.assertEqual(status, 422)
            self.assertIn("message_zh", response["error"])

    def test_failure_durable_and_redacted(self):
        OfflineAgent.failure = True
        trip_id, token = self.create()
        trip = self.completed(trip_id, token)
        self.assertEqual(trip["status"], "failed")
        self.assertNotIn("SECRET", json.dumps(trip))
        self.assertNotIn("/private", json.dumps(trip))
        saved = json.loads((self.service.directory / trip_id / "api-record.json").read_text())
        self.assertEqual(saved["jobs"][-1]["status"], "failed")

    def test_static_capabilities_and_loopback(self):
        self.assertIn(b"studio", self.request("GET", "/")[1])
        self.assertEqual(self.request("GET", "/%2e%2e/data/.service.lock")[0], 404)
        self.assertEqual(self.request("GET", "/.hidden")[0], 404)
        self.assertIn("planner_available", self.request("GET", "/api/agent/capabilities")[1])
        self.assertFalse(self.request("GET", "/api/agent/autopilot/capabilities")[1]["enabled"])
        self.assertFalse(self.request("GET", "/api/maps-config")[1]["configured"])
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            SessionHTTPServer(("0.0.0.0", 0), self.service)

class SessionProgressTests(unittest.TestCase):
    """No listening sockets, workers, model calls or external requests."""

    def setUp(self):
        from unittest.mock import patch
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = SessionService(self.root, lambda _: self.fail("agent instantiated"))
        self.addCleanup(self.service.close)
        with patch.object(self.service.executor, "submit"):
            self.created = self.service.create({"brief": "Offline progress fixture"})
        self.trip_id, self.token = self.created["trip"]["id"], self.created["token"]
        self.directory = self.root / self.trip_id
        self.record = self.service.records[self.trip_id]

    def write(self, name, value):
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def progress(self):
        return self.service.get(self.trip_id, self.token)["trip"]["progress"]

    def activity(self):
        self.write("session/conversation.json", {
            "turns": [{"status": "completed", "started_at": "2026-11-05T01:00:00+00:00",
                       "finished_at": "2026-11-05T01:01:00+00:00"},
                      {"status": "running", "started_at": "2026-11-05T01:02:00+00:00"}],
            "tools": [{"id": str(i), "name": name, "status": status,
                       "started_at": "2026-11-05T09:03:00+08:00",
                       "arguments": {"secret": "PRIVATE"}}
                      for i, (name, status) in enumerate([
                          ("search", "completed"), ("search", "failed"),
                          ("read_page", "completed"), ("record_evidence", "completed"),
                          ("record_evidence", "rejected"), ("save_handbook", "completed"),
                          ("edit_handbook", "running")])]})
        self.write("session/documents/ledger.json", {"evidence": {"E1": {}, "E2": {}, "E3": {}}, "sources": {"s1": {}}})
        self.write("session/documents/versions.json", {"current": 2, "versions": []})
        self.record["jobs"] = [{"kind": "revise", "status": "running",
                                "started_at": "2026-11-05T01:02:00+00:00"}]
        self.service._save(self.record)

    def test_activity_counts_and_ledger_are_independent(self):
        self.activity()
        self.assertEqual(self.progress(), {
            "status": "revising", "completed_turns": 1,
            "tool_counts": {"search": 2, "read_page": 1, "record_evidence": 2,
                            "save_handbook": 1, "edit_handbook": 1},
            "evidence_count": 3, "last_activity_at": "2026-11-05T01:03:00+00:00",
            "document_version": 2})
        self.assertNotIn("PRIVATE", json.dumps(self.progress()))

    def test_missing_and_empty_session(self):
        expected = {"status": None, "completed_turns": 0,
                    "tool_counts": dict.fromkeys(("search", "read_page", "record_evidence", "save_handbook", "edit_handbook"), 0),
                    "evidence_count": 0, "last_activity_at": None, "document_version": 0}
        self.assertEqual(self.progress(), expected)
        self.assertFalse((self.directory / "session").exists())
        self.write("session/conversation.json", {"turns": [], "tools": []})
        self.assertEqual(self.progress(), expected)

    def test_job_states_and_completed_rounds(self):
        self.activity()
        for status, kind, expected in [("running", "generate", "generating"),
                                       ("queued", "revise", "revising"),
                                       ("completed", "revise", "completed"),
                                       ("failed", "generate", "failed"),
                                       ("interrupted", "revise", "failed")]:
            with self.subTest(status=status, kind=kind):
                self.record["jobs"][-1].update(status=status, kind=kind)
                self.service._save(self.record)
                self.assertEqual(self.progress()["status"], expected)
                self.assertEqual(self.progress()["completed_turns"], 1)

    def test_public_contract_additive_and_token_unchanged(self):
        from unittest.mock import patch
        self.assertEqual(set(self.created), {"trip", "token"})
        self.assertNotIn("progress", self.created["trip"])
        baseline = self.service._public(self.record)
        result = self.service.get(self.trip_id, self.token)
        self.assertEqual(set(result), {"trip"})
        progress = result["trip"].pop("progress")
        self.assertEqual(result["trip"], baseline)
        self.assertIn("handbook_url", result["trip"])
        self.assertIsNone(progress["status"])
        with patch.object(self.service, "_progress", side_effect=AssertionError("unauthorized disk read")):
            with self.assertRaises(APIError) as error:
                self.service.get(self.trip_id, "wrong")
            self.assertEqual(error.exception.status, 404)

    def test_get_route_without_socket(self):
        from types import SimpleNamespace
        from travel_agent.single_session_api import SessionHandler
        handler = object.__new__(SessionHandler)
        handler.server = SimpleNamespace(service=self.service, server_address=("127.0.0.1", 7392))
        handler.headers = {"Host": "127.0.0.1:7392", "Authorization": "Bearer " + self.token}
        handler.command = "GET"
        handler.path = f"/api/agent/trips/{self.trip_id}"
        replies = []
        handler._reply = lambda status, value: replies.append((status, value))
        handler._dispatch()
        self.assertEqual(replies, [(200, self.service.get(self.trip_id, self.token))])
        self.assertIn("progress", replies[0][1]["trip"])

    def test_reads_current_disk_records_without_mutation(self):
        self.activity()
        before = {p: p.read_bytes() for p in self.directory.rglob("*.json")}
        first = self.progress()
        self.assertEqual(before, {p: p.read_bytes() for p in self.directory.rglob("*.json")})
        self.write("session/documents/ledger.json", {"evidence": {"E1": {}}})
        self.assertEqual(self.progress()["evidence_count"], 1)
        self.assertEqual(first["evidence_count"], 3)
        self.record["jobs"][-1]["status"] = "completed"  # Not persisted yet.
        self.assertEqual(self.progress()["status"], "revising")

    def test_invalid_optional_json_is_empty(self):
        path = self.directory / "session" / "conversation.json"
        path.parent.mkdir()
        for text in ("{", "null", "[]"):
            path.write_text(text)
            self.assertIsNone(self.progress()["status"])

    def test_failed_job_without_conversation(self):
        self.record["jobs"][-1].update(status="failed", finished_at="2026-11-05T01:05:00+00:00")
        self.service._save(self.record)
        self.assertEqual(self.progress()["status"], "failed")
        self.assertEqual(self.progress()["completed_turns"], 0)

    def test_symlink_outside_data_directory_is_not_read(self):
        path = self.directory / "session"
        path.mkdir()
        with tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / "secret.json"
            secret.write_text('{"turns": [{"status": "completed"}]}')
            (path / "conversation.json").symlink_to(secret)
            self.assertEqual(self.progress()["completed_turns"], 0)


if __name__ == "__main__":
    unittest.main()
