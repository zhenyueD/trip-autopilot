"""Offline reliability preflight; this command has no live-run mode."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.travel_agent.comparison_card_budget import private_json
from packages.travel_agent.run_reliability import configured_run, inspect_run, tokenizer_probe
from packages.travel_agent.itinerary_fixture_preflight import inspect_fixture


def fault_injections():
    # These are deliberately inert credentials, never dispatched to any transport.
    env = dict(DEEPSEEK_API_KEY='offline-fixture-never-sent',
               TRAVEL_WRITER_MODEL='deepseek-v4-pro', TRAVEL_RUN_PYTHON=sys.executable)
    config = configured_run(('assembly', 'review', 'revision'), environ=env)
    cases = []
    altered = dict(env); altered.pop('TRAVEL_WRITER_MODEL')
    cases.append(('missing_writer_model', config, altered, 'required_environment_missing'))
    altered = copy.deepcopy(config); altered['stages'][0]['endpoint'] = 'https://api.openai.com/v1'
    cases.append(('wrong_endpoint', altered, env, 'model_endpoint_mismatch'))
    altered = copy.deepcopy(config); altered['stages'][0]['max_tokens'] -= 1
    cases.append(('undersized_output', altered, env, 'output_cap_below_baseline'))
    altered = copy.deepcopy(config); altered['interpreter'] = '/usr/bin/wrong-python'
    cases.append(('wrong_interpreter', altered, env, 'interpreter_mismatch'))
    positive = inspect_run(config, environ=env)
    results = []
    for name, mutated, variables, code in cases:
        report = inspect_run(mutated, environ=variables)
        results.append(dict(name=name, blocked=not report['passed'], expected_code=code,
            correct_reason=code in {row['code'] for row in report['findings']}, report=report))
    return dict(passed=positive['passed'] and all(row['blocked'] and row['correct_reason'] for row in results),
                positive=positive, injections=results, product_calls=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fault-injections', action='store_true')
    parser.add_argument('--fixture', action='append', default=[])
    parser.add_argument('--blueprint', action='append', default=[])
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.fault_injections:
        result = fault_injections()
    else:
        reports = [inspect_fixture(json.loads(Path(path).read_text()), blueprint=(
            json.loads(Path(args.blueprint[index]).read_text()) if index < len(args.blueprint) else None))
            for index, path in enumerate(args.fixture)]
        result = inspect_run(configured_run(('assembly', 'review', 'revision')),
                             fixture_reports=reports)
    private_json(args.output, result)
    print(json.dumps(dict(passed=result['passed'], product_calls=0, output=args.output)))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
