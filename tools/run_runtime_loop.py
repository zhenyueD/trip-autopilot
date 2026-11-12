"""Explicit runtime entry; no destination rules and no execution without flag."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.travel_agent.comparison_card_budget import private_json
from packages.travel_agent.runtime_loop import RuntimeLoop, local_preflight
from packages.travel_agent.runtime_planning import validate_intake


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--intake', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--revise', action='store_true')
    parser.add_argument('--resume-from', type=Path)
    parser.add_argument('--slim', action='store_true')
    parser.add_argument('--baseline-fixture', type=Path)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    intake = validate_intake(json.loads(args.intake.read_text()))
    if not args.execute:
        result = local_preflight(args.output/'preflight')
        private_json(args.output/'validated-intake.json', intake)
        print(json.dumps(dict(passed=result['passed'], product_calls=0), ensure_ascii=False))
        return 0
    result = asyncio.run(RuntimeLoop(intake, args.output, args.cache, args.resume_from,
        slim=args.slim, baseline_fixture=args.baseline_fixture).run(revise=args.revise))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'delivered_pending_acceptance' else 1


if __name__ == '__main__':
    raise SystemExit(main())
