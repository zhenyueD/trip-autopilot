"""One synthetic trip conversation; revision reopens its actual saved history."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packages'))
from travel_agent.single_session_agent import SingleSessionAgent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--intake', type=Path)
    parser.add_argument('--atom-store', type=Path,
                        help='Shared lifecycle ledger; omitted uses session parent or TRAVEL_ATOM_STORE')
    parser.add_argument('--revision')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--resume-preflight', action='store_true')
    args = parser.parse_args()
    agent = SingleSessionAgent(args.directory, atom_store_path=args.atom_store)
    if args.resume_preflight:
        if not args.execute:
            raise ValueError('Resume requires --execute')
        result = asyncio.run(agent.run_turn(resume_preflight=True))
    elif args.revision:
        if not args.execute:
            raise ValueError('Revision requires --execute; it uses the saved conversation')
        result = asyncio.run(agent.run_turn(args.revision))
    else:
        intake = json.loads(args.intake.read_text())
        intake.pop('revision_feedback', None)
        intake['authority'] = 'CMD-SINGLE-SESSION-AGENT-2026-11-04.md'
        result = agent.preflight(intake)
        if args.execute:
            if agent.session['turns']:
                raise ValueError('Initial trip already started; no silent restart')
            result = asyncio.run(agent.run_turn(json.dumps(intake, ensure_ascii=False), intake=intake))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
