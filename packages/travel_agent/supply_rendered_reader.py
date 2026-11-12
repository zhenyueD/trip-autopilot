"""Bounded unattended DOM acquisition. No model calls or manual capture bridge."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from .comparison_card_budget import now, private_json
from .material_reader import _validate_url, _resolve_address, _GATE


def _stage(name):
    print('[render-stage] ' + json.dumps({'stage': name, 'at': now(),
                                         'monotonic': time.monotonic()}),
          file=sys.stderr, flush=True)


def _trace(stderr):
    events = []
    for line in stderr.splitlines():
        if line.startswith('[render-stage] '):
            try:
                events.append(json.loads(line[len('[render-stage] '):]))
            except json.JSONDecodeError:
                pass
    return events


def _identity(text):
    """Use explicit publisher self-identification, never a supplied entity fact."""
    for line in text.splitlines():
        line = line.strip()
        if (15 <= len(line) <= 350 and
                re.search(r'(?:operated (?:and managed )?by|owned and operated by)',
                          line, re.I) and re.search(r'[A-Za-z]{3}', line)):
            return {'source_type': 'operator', 'source_type_evidence': {
                'quote': line, 'basis': 'rendered publisher self-identification; uncorroborated'}}
    return {'source_type': 'unknown'}


def capture_page(url, timeout_seconds):
    """Executed in an isolated child process with fresh browser state."""
    _stage('playwright_import')
    from playwright.sync_api import sync_playwright
    # The parent already canonicalized HTTP leads; repeat the same URL safety
    # validator here without importing search/model/candidate orchestration.
    requested = _validate_url(url).geturl()
    capture = {'requested_url': requested, 'url': requested, 'text': '',
               'title': '', 'fetched_at': now(), 'status': 'unavailable',
               'redirect_chain': [], 'tool_cost': 'unknown'}
    denied = []
    try:
        _stage('driver_start')
        with sync_playwright() as runtime:
            _stage('browser_launch')
            browser = runtime.chromium.launch(headless=True)
            try:
                _stage('context_setup')
                context = browser.new_context(service_workers='block', accept_downloads=False)
                context.set_default_timeout(timeout_seconds * 1000)

                def route_request(route):
                    request = route.request
                    try:
                        if request.method not in ('GET', 'HEAD'):
                            raise ValueError('read_only_request_method')
                        target = _validate_url(request.url)
                        _resolve_address(target.hostname)
                    except Exception as error:
                        denied.append({'url': request.url, 'reason': str(error)})
                        route.abort()
                        return
                    if request.resource_type in ('media', 'image', 'font'):
                        route.abort()
                    else:
                        route.continue_()

                context.route('**/*', route_request)
                page = context.new_page()
                _stage('navigation')
                response = page.goto(requested, wait_until='domcontentloaded',
                                     timeout=timeout_seconds * 1000)
                _stage('body_read')
                page.locator('body').wait_for()
                # Bounded settle for client-rendered text, never retry navigation.
                page.wait_for_timeout(1500)
                text = page.locator('body').inner_text()
                _stage('metadata_read')
                chain = []
                request = response.request if response else None
                while request and request.redirected_from:
                    previous = request.redirected_from
                    prior_response = previous.response()
                    chain.append({'from_url': previous.url, 'to_url': request.url,
                                  'http_status': prior_response.status if prior_response else None,
                                  'observed': prior_response is not None})
                    request = previous
                capture.update(url=page.url, title=page.title(), text=text,
                               fetched_at=now(), redirect_chain=list(reversed(chain)),
                               visible_links=page.locator('a[href]').evaluate_all(
                                   '(nodes) => nodes.filter(n => n.getClientRects().length).map(n => n.href)'),
                               http_status=response.status if response else None,
                               source_provenance=_identity(text))
                if response is None or response.status >= 400:
                    capture['reason'] = 'http_response_unavailable'
                elif _GATE.search(text):
                    capture['reason'] = 'login_challenge_or_shell'
                elif not text.strip():
                    capture['reason'] = 'visible_text_missing'
                else:
                    capture.update(status='read', content_complete=True)
            finally:
                _stage('browser_close')
                browser.close()
    except Exception as error:
        capture.update(reason='render_failed', raw_error={
            'type': type(error).__name__, 'message': str(error)})
    capture.update(body_length=len(capture['text']), denied_requests=denied)
    _stage('capture_complete')
    return capture


def _subprocess_capture(url, timeout_seconds):
    command = [sys.executable, '-u', '-m', __name__, '--capture', url, str(timeout_seconds)]
    started = time.monotonic()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True,
                               cwd=Path(__file__).resolve().parents[2])
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds + 15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        events = _trace(stderr)
        stage = events[-1]['stage'] if events else 'worker_import'
        error = TimeoutError('render subprocess deadline; stage=' + stage +
                             '; stderr=' + stderr[-2000:])
        error.render_trace = events
        error.timeout_stage = stage
        error.elapsed_ms = round((time.monotonic() - started) * 1000)
        raise error
    if process.returncode:
        raise RuntimeError('render subprocess failed: ' + stderr[-4000:])
    capture = json.loads(stdout)
    capture['render_trace'] = _trace(stderr)
    capture['subprocess_elapsed_ms'] = round((time.monotonic() - started) * 1000)
    return capture


class RenderedReader:
    """Callable compatible with LayeredResearch; each URL is attempted once."""
    def __init__(self, directory, *, timeout_seconds=30, page_limit=6, capture=None):
        if not 1 <= timeout_seconds <= 60 or not 1 <= page_limit <= 6:
            raise ValueError('Invalid rendered reader bounds')
        self.directory = Path(getattr(directory, 'directory', directory))
        self.timeout_seconds, self.page_limit = timeout_seconds, page_limit
        self.capture = capture or _subprocess_capture

    def __call__(self, *, seed, plan, urls, request_id):
        # Planning/provenance live in the long-lived parent. The short-lived
        # capture child must not spend its page deadline importing model code.
        from .deepseek_discovery import DeepSeekSearchResponse, DiscoveryHint
        from .supply_layered_sources import _public_target, rendered_source
        from .supply_read_order import schedule_reads

        directory = self.directory / 'rendered-captures' / hashlib.sha256(
            request_id.encode()).hexdigest()[:20]
        safe, excluded = [], []
        for url in dict.fromkeys(urls):
            try:
                target, _ = _public_target(url)
                if target not in safe:
                    safe.append(target)
            except (ValueError, TypeError, UnicodeError) as error:
                excluded.append({'url': url, 'reason': str(error)})
        response = DeepSeekSearchResponse(tuple(safe), 'response_received',
            tuple(DiscoveryHint('Official website lead', '') for _ in safe))
        schedule = schedule_reads(response, safe)
        receipt = {'request_id': request_id, 'case_id': seed['id'], 'started_at': now(),
                   'status': 'running', 'schedule': schedule, 'excluded': excluded,
                   'product_call_count': 0, 'tool_call_count': 0, 'tool_cost': 'unknown',
                   'captures': [], 'not_attempted': schedule[self.page_limit:]}
        private_json(directory / 'receipt.json', receipt)
        sources, ledger = [], []
        for index, item in enumerate(schedule[:self.page_limit]):
            url = item['requested_url']
            receipt['tool_call_count'] += 1
            private_json(directory / 'receipt.json', receipt)
            try:
                capture = self.capture(url, self.timeout_seconds)
            except Exception as error:
                capture = {'requested_url': url, 'url': url, 'fetched_at': now(),
                           'text': '', 'status': 'unavailable', 'reason': 'render_failed',
                           'raw_error': {'type': type(error).__name__, 'message': str(error)},
                           'render_trace': getattr(error, 'render_trace', []),
                           'timeout_stage': getattr(error, 'timeout_stage', None),
                           'subprocess_elapsed_ms': getattr(error, 'elapsed_ms', None)}
            capture.update(body_length=len(capture.get('text', '')), tool_cost='unknown')
            path = directory / f'capture-{index + 1}.json'
            private_json(path, capture)
            result = rendered_source(capture)
            ledger.append({**result['ledger'], 'capture_path': str(path)})
            receipt['captures'].append({'path': str(path), 'url': capture.get('url'),
                'fetched_at': capture['fetched_at'], 'body_length': capture['body_length'],
                'status': capture['status'], 'raw_error': capture.get('raw_error')})
            if result['source'] is not None:
                sources.append(result['source'])
            private_json(directory / 'receipt.json', receipt)
        receipt.update(status='read' if sources else 'failed', finished_at=now())
        private_json(directory / 'receipt.json', receipt)
        private_json(directory / 'source-ledger.json', ledger)
        return {'sources': sources, 'ledger': ledger, 'tool_receipt': receipt}


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--capture':
        import faulthandler
        faulthandler.dump_traceback_later(float(sys.argv[3]) + 10)
        _stage('worker_ready')
        print(json.dumps(capture_page(sys.argv[2], float(sys.argv[3]))))
        faulthandler.cancel_dump_traceback_later()
    else:
        raise SystemExit('Internal capture worker: --capture URL TIMEOUT')
