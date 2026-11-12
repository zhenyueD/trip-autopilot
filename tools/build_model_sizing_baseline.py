#!/usr/bin/env python3
"""Rebuild the C5/C6 sizing registry using recorded JSON only (no DB or HTTP)."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COHORTS = ('c5-assembly-2026-11-01', 'c5-assembly-second-2026-11-02',
           'c6-fullchain-generalization-2026-11-02')
EVIDENCE = Path('research/run-reliability-baseline-2026-11-03/sizing')
DATA_PATH = Path('packages/travel_agent/model_sizing_baseline.json')
METRICS = ('reasoning_tokens', 'output_tokens', 'input_tokens', 'total_tokens',
           'visible_output_tokens', 'elapsed_ms')
OPERATION_STAGE = {'assemble': 'assembly', 'review': 'review',
                   'revision-demo': 'revision'}


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _number(value):
    return value if type(value) is int and value >= 0 else None


def _usage(usage):
    usage = usage or {}
    details = usage.get('completion_tokens_details') or {}
    result = {'input_tokens': _number(usage.get('prompt_tokens')),
              'output_tokens': _number(usage.get('completion_tokens')),
              'reasoning_tokens': _number(details.get('reasoning_tokens')),
              'total_tokens': _number(usage.get('total_tokens'))}
    output, reasoning = result['output_tokens'], result['reasoning_tokens']
    if output is not None and reasoning is not None and reasoning > output:
        raise ValueError('Reasoning tokens exceed total output tokens')
    result['visible_output_tokens'] = (output - reasoning
        if output is not None and reasoning is not None else None)
    if all(result[k] is not None for k in ('input_tokens', 'output_tokens', 'total_tokens')):
        if result['total_tokens'] != result['input_tokens'] + output:
            raise ValueError('Provider billing total differs from input plus output')
    return result


def _call_rows(value, pointer=''):
    """Read top-level and nested run-stage ledgers; copies remain evidence refs."""
    if isinstance(value, dict):
        for key, child in value.items():
            location = f'{pointer}/{key}'
            if key == 'calls' and isinstance(child, list):
                for index, call in enumerate(child):
                    if isinstance(call, dict) and call.get('request_id'):
                        yield call, f'{location}/{index}'
            elif isinstance(child, (dict, list)):
                yield from _call_rows(child, location)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _call_rows(child, f'{pointer}/{index}')


def discover(root):
    inventory, references, responses = [], {}, {}
    for cohort in COHORTS:
        for path in sorted((root / 'research' / cohort).rglob('*.json')):
            value = _load(path)
            rows = list(_call_rows(value))
            relative = str(path.relative_to(root))
            accounting = bool(rows) or any(term in path.name for term in
                ('account', 'ledger', 'metadata', 'live-execution'))
            role = 'accounting' if accounting else 'other_json_not_a_call_ledger'
            if path.name.endswith('-response.json') and path.parent.name == 'model-artifacts':
                role = 'original_response'
                request_id = path.name.removesuffix('-response.json')
                if request_id in responses:
                    raise ValueError(f'Duplicate original response ID: {request_id}')
                responses[request_id] = (cohort, path, value)
            inventory.append({'path': relative, 'sha256': _sha(path), 'role': role,
                              'call_record_references': len(rows)})
            for call, pointer in rows:
                references.setdefault(call['request_id'], []).append({
                    'path': relative, 'json_pointer': pointer, 'record': call})
    if set(responses) != set(references):
        raise ValueError(f'Unreconciled response/ledger IDs: {set(responses) ^ set(references)}')
    return inventory, references, responses


def _stage(references, request_id):
    stages = {OPERATION_STAGE[ref['record']['operation']] for ref in references
              if ref['record'].get('operation') in OPERATION_STAGE}
    if not stages:
        stages = {OPERATION_STAGE[request_id.rsplit('-', 1)[-1]]}
    if len(stages) != 1:
        raise ValueError(f'Conflicting stages for {request_id}: {stages}')
    return stages.pop()


def _validate_references(refs, response, measured):
    for ref in refs:
        record = ref['record']
        ledger_usage = record.get('raw_response_usage', record.get('usage'))
        if _usage(ledger_usage) != {k: measured[k] for k in _usage(None)}:
            raise ValueError(f'Ledger/raw usage mismatch: {ref["path"]}{ref["json_pointer"]}')
        if record.get('provider_request_id') != response.get('provider_request_id'):
            raise ValueError(f'Ledger/raw provider ID mismatch: {ref["path"]}')
        if record.get('id') and record['id'] != response['lease_id']:
            raise ValueError(f'Ledger/raw physical lease mismatch: {ref["path"]}')


def call_inventory(root, references, responses):
    calls, identities = [], set()
    for request_id, (cohort, path, response) in sorted(responses.items()):
        request_path = path.with_name(f'{request_id}-request.json')
        request = _load(request_path)
        payload = request['body']
        if isinstance(payload, str):
            payload = json.loads(payload)
        body = json.loads(response['body_utf8'])
        measured = _usage(response.get('usage'))
        measured['elapsed_ms'] = _number(response.get('elapsed_ms'))
        refs = references[request_id]
        _validate_references(refs, response, measured)
        identity = response.get('provider_request_id') or response['lease_id']
        if identity in identities:
            raise ValueError(f'Duplicate physical call identity: {identity}')
        identities.add(identity)
        finishes = [choice.get('finish_reason') for choice in body.get('choices', [])]
        run_ids = {ref['record']['run_id'] for ref in refs if ref['record'].get('run_id')}
        calls.append({'request_id': request_id, 'cohort': cohort,
            'run_id': next(iter(run_ids)) if len(run_ids) == 1 else cohort,
            'stage': _stage(refs, request_id), 'lease_id': response['lease_id'],
            'provider_request_id': response.get('provider_request_id'),
            'requested_model': payload.get('model'), 'model': body.get('model'),
            'reasoning_effort': payload.get('reasoning_effort'),
            'http_status': response.get('http_status'), 'finish_reasons': finishes,
            'censored': 'length' in finishes, 'request_max_tokens': payload.get('max_tokens'),
            'response_path': str(path.relative_to(root)), 'response_sha256': _sha(path),
            'request_path': str(request_path.relative_to(root)), 'request_sha256': _sha(request_path),
            'ledger_references': [{'path': ref['path'], 'json_pointer': ref['json_pointer']}
                                  for ref in refs], **measured})
    return calls


def _source(call):
    return {key: call[key] for key in ('run_id', 'request_id', 'response_path',
                                     'response_sha256', 'censored')}


def distribution(calls, metric):
    measured = sorted((call for call in calls if call[metric] is not None),
                      key=lambda call: (call[metric], call['request_id']))
    n = len(measured)
    result = {'n': n, 'unknown': len(calls) - n,
              'censored_observations': sum(call['censored'] for call in measured)}
    for label, quantile in (('p50', .5), ('p90', .9), ('max', 1)):
        rank = math.ceil(quantile * n)
        value = measured[rank - 1][metric] if n else None
        result[label] = {'value': value, 'rank': rank if n else None,
                         'sources': [_source(call) for call in measured if call[metric] == value]}
    return result


def _stage_entry(stage, floor, calls):
    samples = [call for call in calls if call['stage'] == stage]
    stats = {metric: distribution(samples, metric) for metric in METRICS}
    p90 = stats['output_tokens']['p90']['value']
    candidate = math.ceil(p90 * 1.25 / 1000) * 1000 if p90 is not None else None
    return {'stage': stage, 'measurement_status': 'observed_consumption' if samples else 'unmeasured',
        'physical_calls': len(samples), 'censored_calls': sum(call['censored'] for call in samples),
        'config_floor': floor['config_floor'], 'config_floor_provenance': floor,
        'recommended_max_tokens': max(candidate or 0, floor['config_floor']),
        'p90_plus_margin': candidate, 'margin_fraction': .25 if p90 is not None else None,
        'round_up_multiple': 1000 if p90 is not None else None,
        'baseline_floor': max(candidate or 0, floor['config_floor']),
        'basis': ('max(recorded_config_floor, ceil(observed_output_p90 * 1.25 / 1000) * 1000)'
                  if samples else 'recorded_config_floor_only_no_C5_C6_measurement'),
        'statistics': stats,
        'uncensored_statistics': {metric: distribution(
            [call for call in samples if not call['censored']], metric) for metric in METRICS},
        'source_request_ids': [call['request_id'] for call in samples]}


def build(root=ROOT):
    inventory, references, responses = discover(root)
    calls = call_inventory(root, references, responses)
    floors = _load(root / EVIDENCE / 'config-floor-sources.json')
    additions = _load(root / EVIDENCE / 'config-floor-additions-v2.json')
    if _sha(root / EVIDENCE / 'config-floor-sources.json') != additions['original_floor_sources_sha256']:
        raise ValueError('Original configuration floor evidence changed')
    if set(floors) & set(additions['entries']):
        raise ValueError('Supplemental configuration floors cannot overwrite original entries')
    floors.update(additions['entries'])
    stages = {stage: _stage_entry(stage, floor, calls) for stage, floor in floors.items()}
    totals = {metric: {'known_subtotal': sum(call[metric] or 0 for call in calls),
                      'unknown_calls': sum(call[metric] is None for call in calls)} for metric in METRICS}
    baseline = {'schema_version': 1, 'source_cohorts': list(COHORTS),
        'quantile_method': 'nearest_rank: sorted observed values at ceil(p*n); no interpolation',
        'token_semantics': 'Provider output includes reasoning; total=input+output. Visible output=output-reasoning.',
        'censoring_policy': 'Length-truncated calls remain consumed-token observations, not complete-demand measurements. Quantiles may understate future demand.',
        'missing_usage_policy': 'Unknown is null and excluded from quantile n; never replaced with zero.',
        'default_policy': '25% engineering margin, round up to 1000 tokens, retain recorded configuration floor. No proxy p90 for unobserved stages.',
        'limitations': ['Small, non-independent sample of replays/retries, all observed models deepseek-v4-pro; no future success guarantee.',
            'Semantic/schema failures with complete output remain usage observations, not accepted product results.',
            'elapsed_ms is recorded call elapsed time; it is not a socket-read timeout or a recommended timeout. No timeout defaults changed.',
            'Unmeasured stages retain known configuration only; C5/C6 do not validate their limits.',
            'Registry defaults cover model transports and named stages. Explicit legacy per-call token limits remain operation configuration, not additional C5/C6 measurements.'],
        'accounting': {'physical_calls': len(calls), 'cohort_calls': dict(Counter(c['cohort'] for c in calls)),
            'usage_known_calls': sum(c['output_tokens'] is not None for c in calls),
            'usage_unknown_calls': sum(c['output_tokens'] is None for c in calls),
            'censored_calls': sum(c['censored'] for c in calls), 'totals': totals,
            'currency_cost': None, 'currency_cost_status': 'unknown',
            'new_product_calls': 0, 'new_network_calls': 0, 'new_database_calls': 0},
        'stages': stages}
    source = {'source_json_files': len(inventory), 'source_files': inventory,
        'dedup_rule': 'One observation per original response request_id, cross-check unique provider_request_id (or physical lease on HTTP error). All snapshot/run-stage copies are evidence references only. Distinct retry IDs remain distinct physical calls.',
        'ledger_record_references': sum(len(v) for v in references.values()),
        'deduplicated_physical_calls': len(calls), 'calls': calls}
    return baseline, source


def _cell(stat):
    if stat['value'] is None:
        return 'unknown'
    refs = ', '.join(f"{ref['run_id']} / {ref['request_id']}" + (' [length]' if ref['censored'] else '')
                     for ref in stat['sources'])
    return f"{stat['value']:,} ({refs})"


def markdown(baseline, source):
    lines = ['---', 'type: engineering-evidence', 'date: 2026-11-03',
             'tags: [travel-agent, reliability, model-sizing]', 'source: recorded-C5-C6-JSON',
             '---', '', '# C5/C6 model sizing baseline', '',
             baseline['quantile_method'] + '.', '', baseline['token_semantics'], '',
             baseline['censoring_policy'], '', baseline['missing_usage_policy'], '',
             baseline['default_policy'], '',
             '| Stage | Metric | n / unknown / length-censored | p50 (source run / request) | p90 (source run / request) | max (source run / request) |',
             '|---|---|---|---|---|---|']
    for stage, entry in baseline['stages'].items():
        for metric in ('reasoning_tokens', 'output_tokens', 'elapsed_ms'):
            stat = entry['statistics'][metric]
            lines.append(f"| {stage} | {metric} | {stat['n']} / {stat['unknown']} / {stat['censored_observations']} | "
                         + ' | '.join(_cell(stat[key]) for key in ('p50', 'p90', 'max')) + ' |')
    lines += ['', '## Defaults', '',
              '| Stage | Status | Output p90 + 25%, rounded to 1000 | Recorded config floor | Recommended / preflight floor |',
              '|---|---|---|---|---|']
    for stage, entry in baseline['stages'].items():
        lines.append(f"| {stage} | {entry['measurement_status']} | {entry['p90_plus_margin'] or 'unknown'} | {entry['config_floor']} | {entry['recommended_max_tokens']} |")
    lines += ['', '## Physical call inventory', '', source['dedup_rule'], '',
              '| Run | Request | Stage | Input | Output | Reasoning (subset) | Visible | Total | Elapsed ms | Finish / HTTP | Original response |',
              '|---|---|---|---|---|---|---|---|---|---|---|']
    for call in source['calls']:
        values = [str(call[key]) if call[key] is not None else 'unknown' for key in
                  ('input_tokens', 'output_tokens', 'reasoning_tokens', 'visible_output_tokens', 'total_tokens', 'elapsed_ms')]
        lines.append(f"| {call['run_id']} | {call['request_id']} | {call['stage']} | " + ' | '.join(values)
                     + f" | {','.join(call['finish_reasons']) or 'unknown'} / {call['http_status']} | `{call['response_path']}` |")
    lines += ['', '## Accounting and coverage', '', '```json',
              json.dumps(baseline['accounting'], ensure_ascii=False, indent=2), '```', '',
              f"Scanned {source['source_json_files']} JSON files; reconciled {source['ledger_record_references']} ledger record references to {len(source['calls'])} original HTTP responses.", '',
              'The C5-second account-final.json is an earlier one-call failure snapshot; account-completed.json contains all five calls. C6 call-ledger.json and rerun-a/call-ledger-after.json are copies, and run.json repeats calls within stages. No snapshots or shared batch review results count as extra calls.', '',
              'C6 supply_and_blueprint stages have zero physical calls; prior artifacts are reused. A-01 has one assembly call; A-02 has one assembly and one review call. No B live call is present.', '',
              'All original JSON paths, SHA256 hashes and ledger JSON pointers are in source-call-inventory.json. No original evidence was changed. config-floor-sources.json captures pre-change code and inherited configuration sources; config-floor-additions-v2.json adds native_tools without modifying that original input.', '',
              '## Interpretation and next-run risk', '',
              'The highest complete review consumed 31,582 output tokens, close to the old 32,000 cap; review is the clearest measured sizing gap. Assembly has two length-censored records, so its observed p90 is a lower-demand estimate; retaining the existing 36,000 floor avoids lowering the prior configuration on a small replay sample. Revision has only two samples and retains its 12,000 floor.', '',
              'B has no live observation in these ledgers. Its input shape and review demand remain unmeasured; the registry and offline preflight cannot prove B will finish or satisfy itinerary semantics. Any next live run still needs the separately authorized canary, fixture checks and actual result review.', '']
    lines += [f'- {item}' for item in baseline['limitations']]
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--check', action='store_true', help='Compare outputs without writing')
    args = parser.parse_args()
    baseline, source = build(args.root)
    outputs = {DATA_PATH: json.dumps(baseline, ensure_ascii=False, indent=2) + '\n',
        EVIDENCE / 'baseline.json': json.dumps(baseline, ensure_ascii=False, indent=2) + '\n',
        EVIDENCE / 'source-call-inventory.json': json.dumps(source, ensure_ascii=False, indent=2) + '\n',
        EVIDENCE / 'BASELINE.md': markdown(baseline, source)}
    for relative, text in outputs.items():
        path = args.root / relative
        if args.check:
            if not path.exists() or path.read_text(encoding='utf-8') != text:
                raise SystemExit(f'Stale generated artifact: {relative}')
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + '.tmp')
            temporary.write_text(text, encoding='utf-8')
            temporary.replace(path)
    print(json.dumps(baseline['accounting'], ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
