"""Bounded recovery for orchestration search only; accounting stays upstream."""
import hashlib
import threading
import time

from .comparison_card_budget import private_json
from .comparison_card_research import ResearchFailed
from .model import _TransportUnavailable

# Shared across research categories and sessions in this process. Chat keeps the
# existing transport pool; this admission cap only smooths search bursts.
_SEARCH_SLOTS = threading.BoundedSemaphore(3)
RETRY_DELAY_SECONDS = 0.35


def resilient_search(search, arguments, request_id, directory):
    path = directory / 'search' / hashlib.sha256(request_id.encode()).hexdigest()[:16]
    outcome = {'request_id': request_id, 'query': arguments['query'], 'status': 'running',
               'retry_count': 0, 'transport_error_count': 0, 'attempts': []}
    for index in range(2):
        attempt_id = request_id if index == 0 else request_id + ':transport-retry-1'
        attempt = {'request_id': attempt_id, 'status': 'running'}
        outcome['attempts'].append(attempt)
        outcome['retry_count'] = index
        private_json(path / 'transport-status.json', outcome)
        try:
            with _SEARCH_SLOTS:
                result = search(arguments, attempt_id)
        except Exception as error:
            cause = error.__cause__ if isinstance(error, ResearchFailed) else error
            attempt.update(status='failed', error_type=type(error).__name__)
            if not isinstance(cause, _TransportUnavailable):
                outcome['status'] = 'failed'
                private_json(path / 'transport-status.json', outcome)
                raise
            attempt.update(error_code=cause.code, request_delivery_status='not_sent')
            outcome['transport_error_count'] += 1
            outcome['status'] = 'retrying' if index == 0 else 'degraded'
            private_json(path / 'transport-status.json', outcome)
            if index == 0:
                # Release the admission slot during backoff; retries still reserve
                # and settle through the unchanged ComparisonResearch path.
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            outcome['verification_status'] = 'unverified'
            private_json(path / 'transport-status.json', outcome)
            return {'status': 'degraded', 'verification_status': 'unverified',
                    'results': [], 'search_transport': outcome}
        attempt['status'] = 'completed'
        outcome['status'] = 'recovered' if index else 'completed'
        private_json(path / 'transport-status.json', outcome)
        return {**result, 'search_transport': outcome}
