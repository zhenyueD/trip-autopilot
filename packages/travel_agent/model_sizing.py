"""Offline, provenance-bearing model output limits from recorded C5/C6 calls.

Reasoning is included in provider output tokens. An unmeasured stage retains
its recorded configuration floor; it never inherits another stage's quantiles.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path


BASELINE_PATH = Path(__file__).with_name('model_sizing_baseline.json')
_ALIASES = {
    'assemble': 'assembly', 'itinerary_assembly.generate': 'assembly',
    'assembly_and_projection': 'assembly', 'review_batch': 'review',
    'itinerary_assembly.review': 'review', 'readonly_review': 'review',
    'intent_revision': 'revision', 'revision-demo': 'revision',
    'comparison_card': 'comparison', 'supply_layered_plan': 'supply_planner',
    'call_json': 'json',
}


@lru_cache(maxsize=1)
def _runtime_baseline():
    return json.loads(BASELINE_PATH.with_name('runtime_model_sizing_baseline.json').read_text(encoding='utf-8'))


@lru_cache(maxsize=1)
def _baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding='utf-8'))


def baseline_entry(stage: str) -> dict:
    """Return an independent copy, including unknowns and provenance.

    Unknown stage names fail explicitly; silently falling back would conceal
    missing registry coverage from the runner's configuration preflight.
    """
    canonical = _ALIASES.get(stage, stage)
    if canonical in _runtime_baseline()['stages']:
        return deepcopy(_runtime_baseline()['stages'][canonical])
    try:
        return deepcopy(_baseline()['stages'][canonical])
    except KeyError:
        raise ValueError(f'Unregistered model sizing stage: {stage}') from None


def stage_limit(stage: str) -> int:
    """Recommended provider output-token cap, including reasoning tokens."""
    return baseline_entry(stage)['recommended_max_tokens']


def stage_registry() -> dict[str, dict]:
    """Return canonical stages only; aliases resolve through baseline_entry."""
    return {**deepcopy(_baseline()['stages']),
            **deepcopy(_runtime_baseline()['stages'])}
