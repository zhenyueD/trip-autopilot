#!/usr/bin/env python3
"""Freeze independently labeled cases or calculate the fixed Phase 1 metrics."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.travel_agent.uncertainty_evaluation import freeze_cases, summarize


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['freeze', 'metrics'])
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--snapshot-root', type=Path)
    parser.add_argument('--annotator')
    parser.add_argument('--implementation-actor', action='append')
    parser.add_argument('--tasks', type=Path)
    args = parser.parse_args(argv)
    cases = json.loads(args.cases.read_text())
    if args.command == 'freeze':
        if not args.snapshot_root or not args.annotator or not args.implementation_actor:
            parser.error('freeze requires snapshot root, independent annotator and all implementation actors')
        freeze_cases(cases, args.snapshot_root, args.output, annotator=args.annotator,
                     implementation_actors=args.implementation_actor)
    else:
        if not args.tasks:
            parser.error('metrics requires ALL incoming tasks, including failures')
        result = summarize(json.loads(args.tasks.read_text()), cases)
        with args.output.open('x', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
            handle.write('\n')


if __name__ == '__main__':
    main()
