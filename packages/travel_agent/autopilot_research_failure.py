"""Safe, durable research failures; no response text or credentials in receipts."""
import re
from urllib.error import HTTPError

from .phase1_budget_context import BudgetExhausted


def research_failure(error):
    facts = getattr(error, 'usage', {})
    facts = facts if isinstance(facts, dict) else {}
    code = getattr(error, 'code', None)
    if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,99}', code):
        if isinstance(error, BudgetExhausted):
            code = 'BUDGET_EXCEEDED'
        elif isinstance(error, TimeoutError) or isinstance(getattr(error, 'reason', None), TimeoutError):
            code = 'request_timeout'
        else:
            code = 'request_http_error' if isinstance(error, HTTPError) else 'request_transport_error'
    usage = {key: facts.get(key) if type(facts.get(key)) is int and facts[key] >= 0 else None
             for key in ('input_tokens', 'output_tokens')}
    usage['usage_status'] = ('known' if facts.get('usage_complete') is not False
                             and facts.get('usage_status') != 'unknown'
                             and all(v is not None for v in usage.values()) else 'unknown')
    result = dict(status='failed', error_code=code, retryable=False, **usage,
                  request_id=None, provider_request_id=None,
                  request_delivery_status=facts.get('request_delivery_status', 'unknown'),
                  timeout_stage=facts.get('timeout_stage'))
    for key in ('request_id', 'provider_request_id', 'provider_request_id_present',
                'provider_request_id_hmac_sha256', 'provider_acknowledged'):
        if key in facts:
            result[key] = facts[key]
    return result
