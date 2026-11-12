"""Two small, charged provider probes through the unchanged assembly adapter.

This helper does not authorize a live run. Its caller owns authorization and
must use a separate four-request probe ledger and the regular model factory.
Transport injection is for offline tests and is explicitly marked in receipts.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .comparison_card_budget import DurableBudget, private_json
from .itinerary_assembly_model import AssemblyModel, AssemblyNotes, BatchReview
from .itinerary_assembly_pipeline import CRITERIA
from .run_reliability import configured_run, failure_record, require_run_preflight
from .strong_reasoning import strong_reasoning_settings
from .writer_tokenizer import TOKENIZER_REVISION, TOKENIZER_SHA256


class CanaryError(RuntimeError):
    def __init__(self, receipt):
        self.receipt = receipt
        self.failure_class = receipt['failure']['classification']
        super().__init__('Canary rejected before main dispatch: ' + receipt['failure']['code'])


class _Rejected(ValueError):
    def __init__(self, code, classification='config'):
        self.code, self.failure_class = code, classification
        super().__init__(code)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def _artifact(path):
    path = Path(path).resolve()
    return dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def _configuration(model, expected_config):
    if type(model) is not AssemblyModel:
        raise _Rejected('actual_assembly_adapter_required')
    if type(model.budget) is not DurableBudget or model.budget.maximum_requests != 4:
        raise _Rejected('dedicated_four_request_probe_budget_required')
    if model.settings_override:
        raise _Rejected('probe_settings_must_remain_unchanged')
    if model.api_key and model.api_key != os.environ.get('DEEPSEEK_API_KEY'):
        raise _Rejected('actual_key_differs_from_preflight_key_source')
    config = configured_run(('assembly', 'review', 'revision'))
    if expected_config is not None and config != expected_config:
        raise _Rejected('probe_configuration_differs_from_main')
    return config


def _payload(group_id):
    return dict(case_id='canary-' + group_id, title='Inert synthetic canary',
        brief='Synthetic empty contract probe only. No travel facts or real trip. '
              'Keep all lists empty and state the missing evidence without inventing facts.',
        provenance=dict(kind='synthetic_canary_not_product_evidence'),
        confirmed={}, comparison_card={}, intent_card={}, sources=[],
        plan=dict(id='canary-plan-' + group_id, days=[]))


def _review_payload(payload, notes):
    return dict(criteria_ids=list(CRITERIA), cases=[dict(case_id=payload['case_id'],
        sources=[], plan_metadata=payload['plan'],
        handbook=dict(title=payload['title'], claims=notes['claims'],
            sections=dict(daily=[], overview=notes['judgment'], connections=notes['connections'],
                budget=notes['budget'], tasks=notes['tasks'], unknowns=notes['unknowns']),
            audit=dict(provenance=payload['provenance'])))])


def _validate_review(result, payload, notes):
    cases = result['cases']
    if len(cases) != 1 or cases[0]['case_id'] != payload['case_id']:
        raise _Rejected('probe_review_case_binding_failed', 'contract')
    report = cases[0]
    for field, expected in (('criteria', set(CRITERIA)),
                            ('claims', {row['id'] for row in notes['claims']})):
        ids = [row['id'] for row in report[field]]
        if len(ids) != len(set(ids)) or set(ids) != expected:
            raise _Rejected('probe_review_' + field + '_coverage_failed', 'contract')
    # This canary is deliberately an empty product. A live reviewer proves it
    # is judging, rather than rubber-stamping, only by rejecting that product.
    if report['outcome'] != 'failed':
        raise _Rejected('reviewer_rubber_stamp', 'semantic')


def _probe_evidence(model, request_id, stage):
    root = model.budget.directory / 'model-artifacts'
    calls = [row for row in model.budget.snapshot()['calls'] if row['request_id'] == request_id]
    if len(calls) != 1 or calls[0]['status'] != 'response_received':
        raise _Rejected('probe_requires_one_successful_physical_request')
    call = calls[0]
    request = json.loads((root / f'{request_id}-request.json').read_text())
    sizing = json.loads((root / f'{request_id}-preflight.json').read_text())
    config = configured_run((stage,))['stages'][0]
    if (call['requested_model'] != config['model'] or call['model'] != config['model']
            or not call['provider_request_id']
            or request['url'] != config['endpoint'].rstrip('/') + '/chat/completions'):
        raise _Rejected('probe_provider_identity_mismatch')
    settings, body = strong_reasoning_settings(stage), request['body']
    expected = dict(model=config['model'], max_tokens=settings['max_tokens'],
        reasoning_effort=settings['thinking'], thinking=settings['extra_body']['thinking'],
        top_p=settings['top_p'], tool_choice=settings['tool_choice'])
    if any(body.get(key) != value for key, value in expected.items()):
        raise _Rejected('probe_actual_request_settings_mismatch')
    if (sizing.get('preflight_tokenizer_sha256') != TOKENIZER_SHA256
            or sizing.get('preflight_tokenizer_revision') != TOKENIZER_REVISION):
        raise _Rejected('probe_tokenizer_identity_mismatch')
    suffixes = ('request', 'response', 'preflight', 'generated-raw',
                'boundary-normalization', 'boundary-normalized', 'validated')
    return dict(request_id=request_id, provider_request_id=call['provider_request_id'],
        model=call['model'], requested_model=call['requested_model'],
        status='passed', published_tools_sha256=_digest(body['tools']),
        token_usage=call['token_usage'], cost=None, cost_status='unknown',
        artifacts={name: _artifact(root / f'{request_id}-{name}.json') for name in suffixes})


def _execute_probe(model, run_id, stage, mode, payload, receipt, receipt_path):
    request_id = run_id + '-' + stage
    receipt['request_ids'].append(request_id)
    private_json(receipt_path, receipt)
    with model.budget.stage(run_id, stage):
        value = model(request_id=request_id, payload=payload, mode=mode)
        receipt['probes'].append(_probe_evidence(model, request_id, stage))
    private_json(receipt_path, receipt)
    return value


def _failure(error):
    classification = ('config' if type(error).__name__ in (
        'APIConnectionError', 'APITimeoutError', 'APIStatusError', 'AuthenticationError',
        'RateLimitError', 'InternalServerError', 'ConnectError', 'ReadTimeout',
        'ModelConnectionError', 'ModelHTTPError') else None)
    return {**failure_record(error, stage='canary', explicit_class=classification),
            'code': getattr(error, 'code', None) or type(error).__name__}


def run_canary_pair(group_id, model, *, output_dir, fixture=None, approval=None,
                    expected_config=None):
    """Run one assembly and one singleton batch review, without any retry.

    ``model`` must be the actual AssemblyModel, using a shared, probes-only
    DurableBudget(maximum_requests=4). No model callback or CLI fake mode exists.
    A failed/interrupted group cannot be restarted using another output path.
    """
    if group_id not in ('B', 'A'):
        raise ValueError('Canary group must be B or A')
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt_path = directory / 'canary-receipt.json'
    # Exclusive guard prevents replacement of an interrupted or failed receipt.
    with (directory / 'canary-started').open('x') as marker:
        marker.write(group_id + '\n')
    receipt = dict(version=1, group_id=group_id, status='running', passed=False,
        request_ids=[], probes=[], live_canary_verified=False,
        evidence_mode='injected_transport' if getattr(model, 'transport_factory', None)
        else 'live_provider', counts_towards_main_runs=False, cost=None, cost_status='unknown',
        main_dispatch_allowed=False)
    private_json(receipt_path, receipt)
    try:
        config = _configuration(model, expected_config)
        receipt.update(configuration=config, configuration_sha256=_digest(config),
            bindings=dict(fixture_sha256=_digest(fixture), approval_sha256=_digest(approval),
                          fixture_bindings_sha256=_digest(model.fixture_bindings)),
            settings={name: dict(strong_reasoning_settings(name)) for name in ('assembly', 'review')},
            schemas={name: _digest(schema.model_json_schema()) for name, schema in
                     (('AssemblyNotes', AssemblyNotes), ('BatchReview', BatchReview))},
            budget_before=model.budget.snapshot())
        run_id = 'c6-canary-' + group_id
        with model.budget.run(run_id, kind='canary', case_id=group_id,
                             artifact_path=directory / 'probe-run.json'):
            if (fixture is None) != (approval is None):
                raise _Rejected('fixture_and_approval_must_be_paired', 'fixture')
            preflight_path = directory / 'offline-preflight.json'
            preflight = (model.preflight(fixture=fixture, approval=approval, output_path=preflight_path)
                         if fixture is not None else require_run_preflight(
                             config=config, output_path=preflight_path))
            receipt.update(preflight=_artifact(preflight_path), tokenizer=preflight['tokenizer'])
            payload = _payload(group_id)
            notes = _execute_probe(model, run_id, 'assembly', 'assemble', payload, receipt, receipt_path)
            result = _execute_probe(model, run_id, 'review', 'review_batch',
                                    _review_payload(payload, notes), receipt, receipt_path)
            try:
                _validate_review(result, payload, notes)
            except Exception:
                receipt['probes'][-1]['status'] = 'failed'
                raise
        receipt.update(status='passed', passed=True, main_dispatch_allowed=True,
            live_canary_verified=receipt['evidence_mode'] == 'live_provider',
            budget=model.budget.snapshot(), run=_artifact(directory / 'probe-run.json'))
    except Exception as error:
        receipt.update(status='failed', failure=_failure(error))
        if isinstance(getattr(model, 'budget', None), DurableBudget):
            receipt['budget'] = model.budget.snapshot()
            receipt['failure_artifacts'] = [_artifact(path)
                for request_id in receipt['request_ids']
                for path in sorted((model.budget.directory / 'model-artifacts').glob(request_id + '-*.json'))]
        preflight_path = directory / 'offline-preflight.json'
        if preflight_path.exists():
            receipt['preflight'] = _artifact(preflight_path)
        private_json(receipt_path, receipt)
        raise CanaryError(receipt) from error
    private_json(receipt_path, receipt)
    return receipt
