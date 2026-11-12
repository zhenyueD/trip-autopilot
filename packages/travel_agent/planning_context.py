"""Keep prior derived tasks out of planning facts when current intake exists.

This boundary is not a proof of semantic correctness. It prevents old derived
questions and preparation tasks from being treated as current input facts.
Authoritative user wording remains in the material ledger, and real-model
behavior still requires independent regression evaluation.
"""
from copy import deepcopy


def planning_payload(payload: dict) -> dict:
    """Deep-copy context; project only prior plans backed by current intake.

    Full days preserve stable item IDs and locked-item content. All other
    top-level inputs, including feedback, materials, research, source evidence,
    lock IDs and any rejected proposal for constrained repair, stay intact.
    Without a dictionary intake, legacy payloads retain every field.
    """
    context = deepcopy(payload)
    previous_plan = context.get('previous_plan')
    if isinstance(context.get('intake'), dict) and isinstance(previous_plan, dict):
        context['previous_plan'] = {
            key: previous_plan[key]
            for key in ('days', 'signature', 'title')
            if key in previous_plan
        }
    return context
