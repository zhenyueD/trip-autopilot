"""Runtime tiers share the configured provider and evidence-based sizing table."""
from __future__ import annotations

from .model_sizing import baseline_entry
from .strong_reasoning import MODEL, strong_reasoning_settings


FAST_STAGES = {
    'revision_patch': 'revision',
    'supply_planner': 'supply_planner',
    'extraction': 'supply_extraction_fast',
    'supply_extraction_fast': 'supply_extraction_fast',
    'price_binding': 'price_binding_fast',
    'price_binding_fast': 'price_binding_fast',
    'source_binding': 'supply_source_identity',
    'normalization': 'json',
}

INPUT_LIMITS = {'assembly': 45000, 'review': 45000, 'revision': 20000,
                'revision_patch': 20000}
# Recorded v1 wire measurements undercounted provider framing by 347–1322.
# Reserve 2048 rather than presenting the reference encoder as exact billing.
PROTOCOL_RESERVE_TOKENS = 2048


class RuntimeInputLimitExceeded(ValueError):
    pass


def input_admission(stage, measured):
    limit = INPUT_LIMITS[stage]
    observed = measured['local_combined_estimate'] + PROTOCOL_RESERVE_TOKENS
    return dict(stage=stage, input_limit_tokens=limit,
                local_count_plus_reserve=observed,
                protocol_reserve_tokens=PROTOCOL_RESERVE_TOKENS,
                passed=observed <= limit, delivery_truth='not_sent',
                counting_boundary='official reference messages + tools JSON + protocol reserve; provider usage recorded separately')


def model_policy(stage):
    canonical = FAST_STAGES.get(stage, stage)
    entry = baseline_entry(canonical)
    result = dict(stage=stage, sizing_stage=canonical, model=MODEL,
                tier='fast' if stage in FAST_STAGES else 'strong',
                reasoning='disabled' if stage in FAST_STAGES else 'high',
                max_tokens=entry['recommended_max_tokens'], sizing=entry)
    result['input_limit_tokens'] = INPUT_LIMITS.get(stage)
    if stage == 'revision_patch':
        result.update(reasoning_budget_tokens=0,
                      body_reserve_tokens=entry['recommended_max_tokens'],
                      budget_mechanism='thinking disabled; all output capacity reserved for patch body')
    return result


def runtime_model_settings(stage):
    policy = model_policy(stage)
    settings = strong_reasoning_settings(policy['sizing_stage'])
    if policy['tier'] == 'fast':
        settings['thinking'] = False
        settings['extra_body'] = {'thinking': {'type': 'disabled'}}
        # These calls return a declared output tool, never a prose conversation.
        # Required tool selection is compatible with disabled native thinking.
        # PydanticAI's bare 'required' excludes output tools; name its existing
        # final output tool to force structure while allowing this run to end.
        settings['tool_choice'] = ['final_result']
    else:
        settings['thinking'] = 'high'
    return settings
