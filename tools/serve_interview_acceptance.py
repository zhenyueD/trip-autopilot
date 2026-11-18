"""Private real acceptance entry; same product service, isolated data and fee cap.

No model call is made until a user submits through Studio. Run once per approved
new trip. The budget ledger persists across restarts and is never reset here.
"""
import argparse
from functools import partial
import os
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'packages')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--port', type=int, default=7398)
    args = parser.parse_args()
    if args.port in (7377, 7392):
        parser.error('Existing service ports cannot be used for this acceptance')
    root = args.directory.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ['TRAVEL_SESSION_MAX_REQUESTS'] = '50'
    os.environ['TRAVEL_SESSION_INITIAL_REQUESTS'] = '40'
    os.environ['TRAVEL_PDF_PYTHON'] = sys.executable
    # This run only uses known-priced DeepSeek and anonymous public sources.
    for name in ('PEXELS_API_KEY', 'UNSPLASH_ACCESS_KEY', 'GOOGLE_API_KEY', 'JINA_API_KEY'):
        os.environ.pop(name, None)
    from tools.interview_acceptance_budget import install
    budget = install(root / 'acceptance-budget.json')
    from tools import render_service
    from travel_agent import single_session_agent as session
    from travel_agent import supply_render_queue as queue
    render_service.BRIDGE = root / 'render-bridge'
    render_service.DEFAULT_OUT = str(root / 'render-captures')
    session.RenderQueueClient = partial(queue.RenderQueueClient, bridge=render_service.BRIDGE)
    session.research_render_budget = partial(queue.research_render_budget, bridge=render_service.BRIDGE)
    threading.Thread(target=render_service.file_worker, daemon=True).start()
    from travel_agent.single_session_api import SessionService, SessionHTTPServer
    service = SessionService(root / 'sessions', turn_seconds=3600)
    server = SessionHTTPServer(('127.0.0.1', args.port), service, ROOT / 'apps/v1')
    print('Interview acceptance: http://127.0.0.1:' + str(args.port) + '/studio.html', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.close()
        print('Budget receipt persisted; no automatic retry.', flush=True)


if __name__ == '__main__':
    main()
