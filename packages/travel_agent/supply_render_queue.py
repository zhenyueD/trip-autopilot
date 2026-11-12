"""Unattended file-queue client for the ops-owned rendered reader."""
from __future__ import annotations

import json
import math
from pathlib import Path
import time
from uuid import uuid4

from .comparison_card_budget import now, private_json


BRIDGE = Path('/Users/duan/Documents/kimi/Workspaces/旅游agent/research/render-bridge')


class RenderQueueTimeout(TimeoutError):
    """The submitted render has no complete result within the bounded wait."""


class RenderQueueError(RuntimeError):
    """The render service returned an explicit failure for this request."""


def research_render_budget(bridge=BRIDGE):
    """Size a serial queue wait from measured service time and pending pages.

    Calibration: research/render-queue-timeout-2026-11-07/baseline/measurements.json.
    Ten one-page requests, max service 22.934577s, max roundtrip 71.190911s.
    Reserve one additional measured service slot for a late result. The existing
    300s client ceiling bounds total waiting; overload degrades the read.
    """
    pending_pages = 0
    for path in (Path(bridge) / 'inbox').glob('*.json'):
        try:
            request = json.loads(path.read_text())
            pending_pages += min(6, len(request['urls']))
        except (OSError, ValueError, KeyError, TypeError):
            pending_pages += 6  # Unknown request: use the service's page limit.
    late = math.ceil(22.934577)
    wait = min(300 - late, math.ceil(max(10, pending_pages + 1) * 22.934577))
    return {'wait_seconds': wait, 'late_wait_seconds': late,
            'budget_basis': {'policy': 'measured_serial_queue_v1',
                             'sample_requests': 10, 'max_service_seconds': 22.934577,
                             'pending_pages': pending_pages, 'total_wait_cap_seconds': 300}}


class RenderQueueClient:
    def __init__(self, directory, *, bridge=BRIDGE, wait_seconds=300, poll_seconds=0.25,
                 late_wait_seconds=0, budget_basis=None):
        if (not math.isfinite(wait_seconds) or not math.isfinite(poll_seconds)
                or not math.isfinite(late_wait_seconds) or late_wait_seconds < 0
                or not 0 < wait_seconds <= 300 - late_wait_seconds or poll_seconds <= 0):
            raise ValueError('Invalid render queue wait bounds')
        self.directory = Path(directory).resolve()
        self.bridge = Path(bridge)
        self.wait_seconds, self.poll_seconds = wait_seconds, poll_seconds
        self.late_wait_seconds, self.budget_basis = late_wait_seconds, budget_basis

    def __call__(self, *, seed, plan, urls, request_id):
        # A fresh opaque queue ID prevents stale results and path traversal.
        queue_id = 'render-' + uuid4().hex
        evidence = self.directory / 'render-queue' / queue_id
        request = {'request_id': queue_id, 'seed_id': seed['id'],
                   'urls': list(urls), 'out_dir': str(self.directory),
                   'timeout_seconds': 30, 'page_limit': 6}
        receipt = {'request_id': request_id, 'queue_id': queue_id,
                   'started_at': now(), 'status': 'submitted',
                   'product_call_count': 0, 'tool_call_count': 1, 'tool_cost': 'unknown'}
        receipt.update(wait_seconds=self.wait_seconds, late_wait_seconds=self.late_wait_seconds,
                       budget_basis=self.budget_basis, late_wait_used=False)
        private_json(evidence / 'request.json', request)
        private_json(evidence / 'receipt.json', receipt)
        inbox = self.bridge / 'inbox'
        inbox.mkdir(parents=True, exist_ok=True)
        temporary = inbox / (queue_id + '.tmp')
        private_json(temporary, request)
        temporary.rename(inbox / (queue_id + '.json'))
        target = self.bridge / 'outbox' / (queue_id + '.result.json')
        deadline = time.monotonic() + self.wait_seconds
        late_wait_used = False
        while True:
            # Check before the deadline test, including at the final boundary.
            if target.exists():
                raw = target.read_text()
                try:
                    result = json.loads(raw)
                except json.JSONDecodeError:
                    # Service writes directly: a visible file may still be partial.
                    result = None
                if result is not None:
                    private_json(evidence / 'result.json', result)
                    receipt.update(status='error' if 'error' in result else 'received',
                                   finished_at=now(), result_path=str(target))
                    private_json(evidence / 'receipt.json', receipt)
                    if 'error' in result:
                        raise RenderQueueError('render_queue_error: ' + raw)
                    return result
            if time.monotonic() >= deadline:
                if late_wait_used or not self.late_wait_seconds:
                    break
                # Re-poll the SAME queue ID once; never enqueue duplicate work.
                late_wait_used = True
                receipt.update(status='waiting_late', late_wait_used=True,
                               initial_deadline_at=now())
                private_json(evidence / 'receipt.json', receipt)
                deadline += self.late_wait_seconds
            time.sleep(min(self.poll_seconds, max(0, deadline - time.monotonic())))
        receipt.update(status='timeout', finished_at=now(), wait_seconds=self.wait_seconds,
                       reason='render_queue_no_result', result_path=str(target))
        private_json(evidence / 'receipt.json', receipt)
        raise RenderQueueTimeout('render_queue_no_result: ' + queue_id)
