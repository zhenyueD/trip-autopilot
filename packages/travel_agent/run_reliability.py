"""Offline, fail-closed admission shared by paid-run entry points.

This is configuration/contract accounting, not a semantic travel verdict. It
never calls a provider, fixes a fixture or converts a failed review to a pass.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from .comparison_card_budget import private_json

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = Path(__file__).with_name('recorded_contract_samples.json')


class RunPreflightError(ValueError):
    def __init__(self, report):
        self.report = report
        classes = {row['classification'] for row in report['findings']}
        self.failure_class = next(iter(classes)) if len(classes) == 1 else 'config'
        super().__init__('Run preflight rejected before dispatch: ' + ', '.join(
            row['code'] for row in report['findings']))


def failure_record(error, *, stage=None, explicit_class=None):
    """Classify an existing failure without changing its exception or verdict."""
    category = explicit_class or getattr(error, 'failure_class', None)
    facts = getattr(error, 'facts', {})
    code = (facts.get('error_code') if isinstance(facts, dict) else None) or getattr(error, 'code', None)
    name = type(error).__name__
    if category is None:
        if name in ('ValidationError', 'UnexpectedModelBehavior'):
            category = 'contract'
        elif isinstance(error, (TimeoutError, ConnectionError, OSError, ImportError)):
            category = 'config'
        elif name == 'BudgetExhausted' or (code and code in (
                'writer_model_missing', 'writer_key_missing', 'writer_tokenizer_unavailable',
                'invalid_configuration', 'request_timeout', 'network_error', 'http_error')):
            category = 'config'
        elif stage in ('supply_and_blueprint', 'fixture_preflight'):
            category = 'fixture'
        else:
            # Ambiguity is not permission to repair a possible semantic failure.
            category = 'semantic'
    if category not in ('semantic', 'contract', 'config', 'fixture'):
        raise ValueError('Unknown failure accounting category')
    return dict(classification=category, stage=stage, exception_type=name,
                original_code=code, disposition='record_only',
                attribution='explicit' if explicit_class or getattr(error, 'failure_class', None)
                else 'exception_type' if category != 'semantic' else 'semantic_or_unclassified')


def _provider(model, endpoint):
    parsed = urlsplit(endpoint)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    host = parsed.hostname
    if host == 'api.deepseek.com' and parsed.path.rstrip('/') in ('', '/v1') and model.startswith('deepseek-'):
        return 'deepseek'
    if host == 'api.openai.com' and parsed.path.rstrip('/') == '/v1' and model.startswith(('gpt-', 'o1', 'o3', 'o4')):
        return 'openai'
    if (host == 'generativelanguage.googleapis.com' and parsed.path.rstrip('/') == '/v1beta/openai'
            and model.startswith('gemini-')):
        return 'google'
    return None


def _issue(code, path, *, classification='config', **evidence):
    return dict(code=code, path=path, classification=classification, evidence=evidence)


def configured_run(stages, *, environ=None, interpreter=None):
    """Resolve the same fixed native-provider identity used by capability adapters.

    Writer must have an explicit identity; a missing writer model may not inherit
    a critic by accident. Credentials remain in env and never enter this object.
    """
    from .model_sizing import stage_limit
    from .strong_reasoning import MODEL
    env = os.environ if environ is None else environ
    config = dict(interpreter=interpreter or env.get('TRAVEL_RUN_PYTHON'), stages=[], dependencies=[])
    for stage in stages:
        if stage == 'writer':
            config['stages'].append(_writer_configuration(env))
        else:
            config['stages'].append(dict(stage=stage, model=MODEL, endpoint='https://api.deepseek.com',
                provider='deepseek', key_env='DEEPSEEK_API_KEY', key_owner='deepseek',
                max_tokens=stage_limit(stage), required_env=['DEEPSEEK_API_KEY']))
    if 'writer' not in stages:
        # Required configuration dependency, not a legacy writer model call in
        # a C5/C6 assembly/review/revision schedule. Never claim a writer replay.
        config['dependencies'].append(_writer_configuration(env))
    return config


def _writer_configuration(env):
    from .model import writer_settings
    from .model_sizing import stage_limit
    try:
        actual = writer_settings(environ=env)
        error = None
    except Exception as exc:
        actual = {'model': '', 'base': '', 'key': None}
        error = type(exc).__name__
    key = actual.get('key')
    known = {'DEEPSEEK_API_KEY': 'deepseek', 'OPENAI_API_KEY': 'openai', 'GOOGLE_API_KEY': 'google'}
    owner = next((owner for name, owner in known.items() if key and env.get(name) == key), None)
    key_env = next((name for name in ('TRAVEL_WRITER_API_KEY', 'TRAVEL_AGENT_API_KEY', *known)
                    if key and env.get(name) == key), '')
    # Dedicated keys can only claim their explicitly configured provider; no
    # prefix inference or network auth check is smuggled into an offline probe.
    owner = owner or env.get('TRAVEL_WRITER_KEY_PROVIDER')
    return dict(stage='writer', model=actual['model'], endpoint=actual['base'],
        provider=_provider(actual['model'], actual['base']) or 'unknown', key_env=key_env,
        key_owner=owner, max_tokens=stage_limit('writer'), configuration_error=error,
        required_env=['TRAVEL_WRITER_MODEL'] + ([key_env] if key_env else []))


def tokenizer_probe():
    """Exercise the installed interpreter, pinned asset and complete tool dialogue."""
    from .autopilot_tokenizer import measure_native_input
    from .writer_tokenizer import measure_writer_input
    messages = [{'role': 'system', 'content': 'Offline preflight.'},
                {'role': 'user', 'content': 'Measure this request.'}]
    writer = measure_writer_input(messages, model='deepseek-v4-pro', reasoning_effort='max')
    tools = [{'type': 'function', 'function': {'name': 'inspect', 'parameters': {
        'type': 'object', 'properties': {}, 'additionalProperties': False}}}]
    full = messages + [dict(role='assistant', content=None, tool_calls=[dict(
        id='offline-probe', type='function', function=dict(name='inspect', arguments='{}'))]),
        dict(role='tool', tool_call_id='offline-probe', content='{"status":"recorded"}')]
    native = measure_native_input(full, tools=tools, model='deepseek-v4-pro', reasoning_effort='max')
    return dict(writer=writer, native_tool_dialogue=native, product_calls=0)


def replay_contract_samples(samples=None, *, required_stages=()):
    """Replay immutable provider arguments through unchanged output schemas."""
    from .model_boundary import normalize_model_output
    if samples is None:
        samples = json.loads(REGISTRY.read_text())['samples']
    records = []
    for sample in samples:
        try:
            path = ROOT / sample['path']
            blob = path.read_bytes()
            if hashlib.sha256(blob).hexdigest() != sample['sha256']:
                raise ValueError('Recorded response hash changed')
            raw = json.loads(blob)
            for key in sample.get('value_path', []):
                raw = raw[key]
            if sample.get('json_arguments'):
                raw = json.loads(raw)
            module, name = sample['schema'].rsplit(':', 1)
            schema = getattr(importlib.import_module(module), name)
            normalized, audit = normalize_model_output(raw, schema.model_json_schema(),
                                                       request_id=sample['request_id'])
            value = schema.model_validate(normalized)
            records.append(dict(stage=sample['stage'], path=sample['path'],
                sha256=sample['sha256'], passed=True, changed=normalized != raw,
                audit=audit, outcome=value.model_dump().get('outcome'),
                schema=sample['schema']))
        except Exception as error:
            records.append(dict(stage=sample.get('stage'), path=sample.get('path'),
                                passed=False, error_type=type(error).__name__))
    covered = {row['stage'] for row in records if row['passed']}
    for stage in set(required_stages) - covered:
        records.append(dict(stage=stage, passed=False, error_type='RecordedContractMissing'))
    return records


def inspect_run(config, *, environ=None, actual_interpreter=None, probe=None,
                contract_replay=None, fixture_reports=()):
    """Pure report plus offline tokenizer/schema checks. No dispatch or credentials."""
    from .model_sizing import baseline_entry, stage_limit
    env = os.environ if environ is None else environ
    findings, limits = [], []
    expected = config.get('interpreter')
    actual = actual_interpreter or sys.executable
    # Compare invocation paths, not resolved symlinks: two venvs can share a binary.
    if not expected or os.path.abspath(expected) != os.path.abspath(actual):
        findings.append(_issue('interpreter_mismatch', 'interpreter', expected=expected, actual=actual))
    stages = config.get('stages', [])
    if not stages:
        findings.append(_issue('stages_missing', 'stages'))
    for index, item in enumerate(stages + config.get('dependencies', [])):
        path = f'stages[{index}]'
        for name in item.get('required_env', []):
            if not env.get(name):
                findings.append(_issue('required_environment_missing', path, variable=name))
        provider = _provider(item.get('model', ''), item.get('endpoint', ''))
        if provider is None or provider != item.get('provider'):
            findings.append(_issue('model_endpoint_mismatch', path, provider=item.get('provider')))
        if item.get('key_owner') != provider or not env.get(item.get('key_env', '')):
            findings.append(_issue('credential_owner_mismatch', path, key_source=item.get('key_env')))
        try:
            minimum = stage_limit(item['stage'])
            entry = baseline_entry(item['stage'])
            cap = item.get('max_tokens')
            if type(cap) is not int or cap < minimum:
                findings.append(_issue('output_cap_below_baseline', path, configured=cap, minimum=minimum))
            limits.append(dict(stage=item['stage'], configured=cap, minimum=minimum, baseline=entry))
        except (KeyError, ValueError):
            findings.append(_issue('stage_baseline_missing', path, stage=item.get('stage')))
    try:
        measured = (probe or tokenizer_probe)()
    except Exception as error:
        measured = {'error_type': type(error).__name__}
        findings.append(_issue('tokenizer_preflight_failed', 'tokenizer', error_type=type(error).__name__))
    try:
        required = {baseline_entry(row['stage'])['stage'] for row in stages}
        contracts = (contract_replay() if contract_replay else
                     replay_contract_samples(required_stages=required))
        covered = {row.get('stage') for row in contracts if row.get('passed') is True}
        for stage in required - covered:
            contracts.append(dict(stage=stage, passed=False, error_type='RecordedContractMissing'))
        if not contracts or any(row.get('passed') is not True for row in contracts):
            findings.append(_issue('recorded_schema_contract_failed', 'contracts', classification='contract'))
    except Exception as error:
        contracts = [{'passed': False, 'error_type': type(error).__name__}]
        findings.append(_issue('recorded_schema_contract_failed', 'contracts', classification='contract'))
    for index, report in enumerate(fixture_reports):
        if report.get('passed') is not True:
            findings.append(_issue('frozen_fixture_inconsistent', f'fixtures[{index}]',
                                   classification='fixture', findings=report.get('findings', [])))
    return dict(passed=not findings, findings=findings, limits=limits, tokenizer=measured,
        contracts=contracts, fixtures=list(fixture_reports), product_calls=0,
        provider_credential_validation='local_configuration_only_not_remote_authentication',
        live_canary_status='not_run_requires_next_live_authorization')


def require_run_preflight(*, stages=('assembly', 'review', 'revision'),
                          fixtures=(), approvals=(), output_path=None, config=None,
                          environ=None, fixture_bindings=(), **checks):
    from .itinerary_fixture_preflight import inspect_fixture
    config = copy.deepcopy(config) if config is not None else configured_run(stages, environ=environ)
    reports = [inspect_fixture(value, blueprint=(approvals[index].get('blueprint')
               if index < len(approvals) else None)) for index, value in enumerate(fixtures)]
    report = inspect_run(config, environ=environ, fixture_reports=reports, **checks)
    if fixture_bindings:
        _apply_fixture_dispositions(report, fixtures, fixture_bindings)
    elif not isinstance(fixture_bindings, (tuple, list)):
        report['findings'].append(_issue('fixture_bindings_invalid', 'fixture_bindings',
                                        classification='fixture'))
        report['passed'] = False
    if output_path is not None:
        private_json(output_path, report)
    for row in report['limits']:
        print(json.dumps(dict(stage=row['stage'], max_tokens=row['configured'],
            minimum=row['minimum'], measurement_status=row['baseline']['measurement_status'],
            source_request_ids=row['baseline'].get('source_request_ids', [])), ensure_ascii=False), flush=True)
    if not report['passed']:
        raise RunPreflightError(report)
    return report


def _apply_fixture_dispositions(report, fixtures, bindings):
    from .fixture_dispositions import consume_dispositions
    report['fixture_dispositions'] = []
    if not isinstance(bindings, (tuple, list)) or len(bindings) != len(fixtures):
        report['findings'].append(_issue('fixture_bindings_count_mismatch', 'fixture_bindings',
                                        classification='fixture'))
        report['passed'] = False
        return
    for index, (fixture, raw, binding) in enumerate(zip(fixtures, report['fixtures'], bindings)):
        receipt = consume_dispositions(fixture, raw, binding)
        report['fixture_dispositions'].append(receipt)
        path = f'fixtures[{index}]'
        if receipt['admitted']:
            report['findings'] = [row for row in report['findings']
                if not (row['code'] == 'frozen_fixture_inconsistent' and row['path'] == path)]
        else:
            report['findings'].append(_issue('fixture_disposition_invalid', path,
                classification='fixture', errors=receipt['errors']))
    report['passed'] = not report['findings']
