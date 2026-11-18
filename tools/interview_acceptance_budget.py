"""One-process, external envelope for the approved interview acceptance run.

Install before constructing SessionService/RuntimeMediaProvider. This does not
change model parameters or the product's own accounting. Public reference input
counts and peak uncached prices are estimates, not a provider-enforced spend cap
or an actual bill. In-flight requests/threads cannot be revoked by this adapter.
"""
from __future__ import annotations

import copy
from functools import wraps
import hashlib
import importlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time


LIMITS = {'requests': 50, 'input_tokens': 2_000_000,
          'output_tokens': 240_000, 'seconds': 5400, 'cost_micro_cny': 50_000_000}
# Official CNY peak prices, checked 2026-11-18. Cache savings are not assumed.
PRICE_SOURCE = 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'
INPUT_MICRO_CNY, OUTPUT_MICRO_CNY, SEARCH_MICRO_CNY = 9, 27, 5000
MODEL = 'deepseek-v4-pro'
ENDPOINT = 'https://api.deepseek.com/chat/completions'
SEARCH_ENDPOINT = 'https://api.deepseek.com/v1/tools/search'
_installed = None
_install_lock = threading.RLock()


class AcceptanceBudgetExceeded(RuntimeError):
    pass


def _integer(value):
    return type(value) is int and value >= 0


def _cost(input_tokens, output_tokens):
    return input_tokens * INPUT_MICRO_CNY + output_tokens * OUTPUT_MICRO_CNY


class Envelope:
    """Atomic durable reservations shared by native async and media/search threads."""

    def __init__(self, path, *, limits=None, clock=time.time):
        self.path, self.clock = Path(path).resolve(), clock
        self.lock = threading.RLock()
        self.persistence_failed = False
        self.limits = dict(LIMITS if limits is None else limits)
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            if self.state.get('schema') != 1 or self.state.get('limits') != self.limits:
                raise AcceptanceBudgetExceeded('acceptance_ledger_configuration_mismatch')
            # A prior process may have dispatched any outstanding reservation.
            for entry in self.state['entries']:
                if entry['status'] == 'reserved':
                    entry.update(status='unknown', reason='process_restarted_before_settlement')
        else:
            self.state = {'schema': 1, 'started_at': None,
                          'limits': self.limits, 'entries': []}
        self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=self.path.name + '.', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(self.state, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        except BaseException:
            self.persistence_failed = True
            raise
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _totals(self):
        entries = self.state['entries']
        return {'requests': len(entries), **{
            key: sum(entry[key] or 0 for entry in entries)
            for key in ('input_tokens', 'output_tokens', 'cost_micro_cny')}}

    def _stop_reason(self):
        if self.persistence_failed:
            return 'ledger_persistence_failed'
        if any(entry['status'] == 'unknown' for entry in self.state['entries']):
            return 'unknown_usage'
        if any(entry.get('estimate_exceeded') for entry in self.state['entries']):
            return 'provider_usage_exceeded_reservation'
        if (self.state['started_at'] is not None
                and self.clock() - self.state['started_at'] >= self.limits['seconds']):
            return 'elapsed_seconds'
        return None

    def reserve(self, kind, *, input_tokens=0, output_tokens=0, preflight=None):
        if kind not in ('native', 'media', 'search'):
            raise AcceptanceBudgetExceeded('unpriced_request_kind')
        if not _integer(input_tokens) or not _integer(output_tokens):
            raise AcceptanceBudgetExceeded('invalid_token_reservation')
        if kind != 'search' and (not input_tokens or not output_tokens):
            raise AcceptanceBudgetExceeded('missing_token_reservation')
        cost = SEARCH_MICRO_CNY if kind == 'search' else _cost(input_tokens, output_tokens)
        with self.lock:
            reason = self._stop_reason()
            if reason:
                raise AcceptanceBudgetExceeded('acceptance_budget_' + reason)
            totals = self._totals()
            additions = dict(requests=1, input_tokens=input_tokens,
                             output_tokens=output_tokens, cost_micro_cny=cost)
            for key, amount in additions.items():
                if totals[key] + amount > self.limits[key]:
                    raise AcceptanceBudgetExceeded('acceptance_budget_' + key)
            if self.state['started_at'] is None:
                self.state['started_at'] = self.clock()
            entry = {'id': len(self.state['entries']) + 1, 'kind': kind,
                     'status': 'reserved', 'reserved_at': self.clock(),
                     'input_tokens': None if kind == 'search' else input_tokens,
                     'output_tokens': None if kind == 'search' else output_tokens,
                     'cost_micro_cny': cost, 'actual_input_tokens': None,
                     'actual_output_tokens': None, 'preflight': copy.deepcopy(preflight)}
            entry.update(reserved_input_tokens=entry['input_tokens'],
                         reserved_output_tokens=entry['output_tokens'],
                         reserved_cost_micro_cny=cost)
            self.state['entries'].append(entry)
            self._save()  # Reservation must be durable before dispatch.
            return entry['id']

    def settle(self, ident, usage=None, *, response_model=None, reason=None):
        with self.lock:
            entry = self.state['entries'][ident - 1]
            if entry['status'] != 'reserved':
                raise AcceptanceBudgetExceeded('acceptance_duplicate_settlement')
            valid_model = response_model in (None, MODEL, 'deepseek-v4-pro-0813')
            if entry['kind'] == 'search':
                entry.update(status='settled', token_usage='not_applicable_request_priced',
                             response_status='failed' if reason else 'received',
                             error_type=reason, settled_at=self.clock())
            elif (reason is None and valid_model and isinstance(usage, dict)
                  and _integer(usage.get('prompt_tokens'))
                  and _integer(usage.get('completion_tokens'))):
                input_tokens, output_tokens = usage['prompt_tokens'], usage['completion_tokens']
                entry.update(estimate_exceeded=(input_tokens > entry['input_tokens']
                    or output_tokens > entry['output_tokens']), status='settled',
                    actual_input_tokens=input_tokens, actual_output_tokens=output_tokens,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    cost_micro_cny=_cost(input_tokens, output_tokens),
                    response_model=response_model, usage=copy.deepcopy(usage),
                    settled_at=self.clock())
            else:
                # Keep the maximum reservation: absence of usage never means zero.
                entry.update(status='unknown', reason=reason or 'missing_or_invalid_usage',
                             usage=copy.deepcopy(usage), settled_at=self.clock())
            self._save()

    def receipt(self):
        with self.lock:
            totals = self._totals()
            return {'limits': {**self.limits, 'cost_cny': self.limits['cost_micro_cny'] / 1e6},
                'totals_including_reservations': totals,
                'estimated_cost_upper_cny': totals['cost_micro_cny'] / 1e6,
                'actual_bill_cny': None, 'actual_bill_status': 'unknown',
                'elapsed_seconds': (0 if self.state['started_at'] is None else
                                    max(0, self.clock() - self.state['started_at'])),
                'stop_reason': self._stop_reason(), 'ledger': str(self.path),
                'unknown_requests': sum(e['status'] == 'unknown' for e in self.state['entries']),
                'unsettled_requests': sum(e['status'] == 'reserved' for e in self.state['entries']),
                'pricing': {'source': PRICE_SOURCE, 'checked_on': '2026-11-18',
                    'input_cny_per_million': 9, 'output_cny_per_million': 27,
                    'search_cny_per_request': .005, 'cache_savings_assumed': False},
                'bound_basis': 'public_reference_input_and_requested_max_output; provider_usage_may_differ'}


def _provider(endpoint, model):
    if endpoint != ENDPOINT or model != MODEL:
        raise AcceptanceBudgetExceeded('unpriced_provider_or_model')


def _media_preflight(module, tokenizer, context):
    messages = [{'role': 'system', 'content': module.SYSTEM,
                 'response_format': {'type': 'json_object'}},
                {'role': 'user', 'content': json.dumps(module.model_context(context),
                    ensure_ascii=False, separators=(',', ':'))}]
    prompt = tokenizer._load_encoder().encode_messages(
        messages, thinking_mode='chat', drop_thinking=False, reasoning_effort=None)
    return {**tokenizer._base_facts(), 'preflight_input_tokens': len(
        tokenizer._load_tokenizer().encode(prompt, add_special_tokens=False).ids),
        'preflight_prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
        'preflight_message_count': 2, 'preflight_thinking_mode': 'chat',
        'preflight_response_format': {'type': 'json_object'}}


def install(path, *, package='travel_agent'):
    """Install once, before constructing the server; resume the same ledger on restart.

    Normal server imports use travel_agent. package='packages.travel_agent' is
    only for harnesses already using that repository import convention.
    """
    global _installed
    with _install_lock:
        if _installed is not None:
            if _installed.path != Path(path).resolve() or _installed.package != package:
                raise AcceptanceBudgetExceeded('acceptance_envelope_already_installed')
            return _installed
        if package not in ('travel_agent', 'packages.travel_agent'):
            raise AcceptanceBudgetExceeded('unsupported_product_import')
        agent = importlib.import_module(package + '.single_session_agent')
        media = importlib.import_module(package + '.single_session_media_selection')
        discovery = importlib.import_module(package + '.deepseek_discovery')
        tokenizer = importlib.import_module(package + '.autopilot_tokenizer')
        _provider(agent.ENDPOINT, agent.MODEL)
        if discovery.API != SEARCH_ENDPOINT:
            raise AcceptanceBudgetExceeded('unpriced_search_endpoint')
        envelope = Envelope(path)
        envelope.package = package
        native_call = agent.NativeRecordedTransport.handle_async_request
        media_call, search_call = media.product_model, discovery.DeepSeekDiscovery.__call__

        @wraps(native_call)
        async def native(self, request):
            payload = tokenizer.strict_json(request.content)
            _provider(str(request.url), payload.get('model'))
            if request.method != 'POST' or payload.get('reasoning_effort') not in ('low', 'high'):
                raise AcceptanceBudgetExceeded('unexpected_native_request_mode')
            measured = agent.measure_session_input(payload, official_deepseek=True)
            ident = envelope.reserve('native', input_tokens=measured['preflight_input_tokens'],
                                     output_tokens=payload['max_tokens'], preflight=measured)
            try:
                response = await native_call(self, request)
                try:
                    data = response.json()
                except (ValueError, TypeError):
                    data = {}
                if not isinstance(data, dict):
                    data = {}
            except BaseException as error:
                envelope.settle(ident, reason=type(error).__name__)
                raise
            envelope.settle(ident, data.get('usage'), response_model=data.get('model'))
            return response

        @wraps(media_call)
        def media_model(context):
            _provider(agent.ENDPOINT, agent.MODEL)
            measured = _media_preflight(media, tokenizer, context)
            ident = envelope.reserve('media', input_tokens=measured['preflight_input_tokens'],
                                     output_tokens=8192, preflight=measured)
            try:
                result = media_call(context)
            except BaseException as error:
                facts = getattr(error, 'usage', {}) or {}
                if not isinstance(facts, dict):
                    facts = {}
                envelope.settle(ident, facts.get('tokens'), response_model=facts.get('response_model'))
                raise
            facts = result[1]
            if not isinstance(facts, dict):
                facts = {}
            envelope.settle(ident, facts.get('tokens'), response_model=facts.get('response_model'))
            return result

        @wraps(search_call)
        def search(self, *args, **kwargs):
            if discovery.API != SEARCH_ENDPOINT:
                raise AcceptanceBudgetExceeded('unpriced_search_endpoint')
            ident = envelope.reserve('search')
            try:
                result = search_call(self, *args, **kwargs)
            except BaseException as error:
                envelope.settle(ident, reason=type(error).__name__)
                raise
            envelope.settle(ident)
            return result

        agent.NativeRecordedTransport.handle_async_request = native
        media.product_model = media_model
        discovery.DeepSeekDiscovery.__call__ = search
        _installed = envelope
        return envelope
