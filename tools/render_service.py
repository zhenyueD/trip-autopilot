#!/usr/bin/env python3
"""Localhost render service: runs the pipeline RenderedReader outside any
agent sandbox (Chromium cannot launch inside the Codex seatbelt sandbox:
macOS bootstrap_check_in Permission denied (1100)).

Ops-owned (started/stopped by Muse via SSH). Binds 127.0.0.1 only.

Channels:
1. HTTP  POST /render  {urls, request_id, seed_id?, out_dir?, timeout_seconds?, page_limit?}
         -> {sources, ledger, tool_receipt}
         GET  /health   -> {"ok": true, ...}
2. File queue (fallback when sandbox blocks localhost HTTP):
   drop a request JSON named <request_id>.json into BRIDGE/inbox/
   (same fields as /render body); result appears at BRIDGE/outbox/<request_id>.result.json
   and the request file moves to BRIDGE/done/.
   Producers should write to a .tmp name first and rename into inbox/
   (atomic); the worker also stability-checks size/mtime before reading.

Captures land under out_dir (default: main repo research/external-render-captures).
URL safety is enforced inside RenderedReader itself (public-target validation,
read-only methods, DNS checks); this service adds no fetching logic of its own.
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.travel_agent.supply_rendered_reader import RenderedReader

MAIN_RESEARCH = Path("/Users/duan/Documents/kimi/Workspaces/旅游agent/research")
DEFAULT_OUT = str(MAIN_RESEARCH / "external-render-captures")
BRIDGE = MAIN_RESEARCH / "render-bridge"
_lock = threading.Lock()


def run_render(req):
    started = time.monotonic()
    context = {"request_id": req["request_id"], "pid": os.getpid(),
               "timeout_seconds": int(req.get("timeout_seconds", 30)),
               "page_limit": int(req.get("page_limit", 6))}
    print(json.dumps({**context, "event": "render_started", "at": time.time()}), flush=True)
    reader = RenderedReader(
        req.get("out_dir", DEFAULT_OUT),
        timeout_seconds=context["timeout_seconds"],
        page_limit=context["page_limit"],
    )
    try:
        result = reader(seed={"id": req.get("seed_id", req["request_id"])},
                        plan={}, urls=req["urls"], request_id=req["request_id"])
    except Exception as error:
        print(json.dumps({**context, "event": "render_error",
                          "error_type": type(error).__name__,
                          "elapsed_ms": round((time.monotonic() - started) * 1000)}), flush=True)
        raise
    receipt = result['tool_receipt']
    print(json.dumps({**context, "event": "render_finished",
                      "elapsed_ms": round((time.monotonic() - started) * 1000),
                      "status": receipt['status'],
                      "body_lengths": [x['body_length'] for x in receipt['captures']]}), flush=True)
    return result


def _stable(path):
    """True when the file looks fully written: non-empty, mtime >= 2s old,
    and size unchanged across a short interval."""
    try:
        stat = path.stat()
        if stat.st_size == 0 or time.time() - stat.st_mtime < 2:
            return False
        size = stat.st_size
        time.sleep(0.3)
        return path.stat().st_size == size
    except OSError:
        return False


def file_worker():
    inbox, outbox, done = BRIDGE / "inbox", BRIDGE / "outbox", BRIDGE / "done"
    for d in (inbox, outbox, done):
        d.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            for path in sorted(inbox.glob("*.json")):
                if not _stable(path):
                    continue
                try:
                    req = json.loads(path.read_text())
                    with _lock:
                        result = run_render(req)
                    target = outbox / (path.stem + ".result.json")
                    target.write_text(json.dumps(result, ensure_ascii=False, default=str))
                except Exception as error:  # keep the queue alive; report per-request
                    target = outbox / (path.stem + ".result.json")
                    target.write_text(json.dumps(
                        {"error": "render_failed",
                         "detail": f"{type(error).__name__}: {error}"},
                        ensure_ascii=False))
                finally:
                    try:
                        path.rename(done / path.name)
                    except OSError:
                        pass
        except Exception:
            pass
        time.sleep(1)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True, "service": "render-service", "root": str(ROOT),
                             "bridge": str(BRIDGE)})
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/render":
            self._json(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length) or b"{}")
            _ = req["urls"], req["request_id"]
        except Exception as error:
            self._json(400, {"error": "bad_request", "detail": str(error)})
            return
        with _lock:
            try:
                self._json(200, run_render(req))
            except Exception as error:
                self._json(500, {"error": "render_failed",
                                 "detail": f"{type(error).__name__}: {error}"})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7391
    threading.Thread(target=file_worker, daemon=True).start()
    print(f"render-service listening on 127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
