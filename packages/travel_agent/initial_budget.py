"""Optional first-plan enrichment must leave room for delivery review."""
from contextlib import contextmanager

from .phase1_budget_context import current_budget, use_budget

DELIVERY_REVIEW_CALLS = 4  # Proposal, critic, one local warning repair, final critic.
DELIVERY_REVIEW_SECONDS = 180

@contextmanager
def optional_work(usage, stage):
    budget = current_budget()
    initial = usage.get('direction_policy') == 'initial' or usage.get('initial_generation')
    if budget is None or not initial or budget.level != 'L0':
        yield True
        return
    calls = max(0, budget.snapshot()['calls_remaining'] - DELIVERY_REVIEW_CALLS)
    seconds = min(45, max(0, budget.remaining_seconds - DELIVERY_REVIEW_SECONDS))
    if not calls or not seconds:
        usage.setdefault('optional_work_deferred', []).append(stage)
        yield False
        return
    child = budget.child('L1', 'initial-' + stage, seconds=seconds, max_calls=calls)
    try:
        with use_budget(child):
            yield True
    finally:
        usage.setdefault('optional_work_budgets', {})[stage] = child.snapshot()
