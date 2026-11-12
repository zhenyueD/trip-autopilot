"""Bounded result-driven work. Every executed step emits a durable checkpoint.

The model chooses useful work; this deterministic harness enforces tool scope,
bounded cost and evidence statuses. It cannot order, pay, or contact suppliers.
"""
from __future__ import annotations

import copy
import json

from . import atomic_prompts, model
from .runtime import add_failed_usage, add_usage
from . import initial_public_research
from .research_focus import (compile_research_focus, authorize_research_action, blocked_gap,
                             admit_discovered_candidates, recent_research)

TOOLS = {'search_supply', 'search_public', 'inspect_source', 'inspect_geography'}
MAX_ROUNDS = 3
MAX_ACTIONS = 2


def _string(value, limit=1200):
    return value[:limit] if isinstance(value, str) else ''


def _blocked(action, detail, index):
    return {'id': f'work-blocked-{index}', 'title': _string(action.get('reason'), 100) or '尚未完成的工作',
            'kind': _string(action.get('tool'), 60) or 'capability_gap', 'status': 'blocked',
            'detail': detail, 'supply_ids': [], 'evidence_ids': []}


def _work_context(direction, context, state):
    fields = ('id', 'name', 'kind', 'city', 'description', 'constraints', 'source_url')
    from .capability_tools import _sources
    known_urls = _sources(state['supplies'])
    previous = context.get('previous_plan') or {}
    focus = state.get('research_focus')
    supplies = [s for s in state['supplies'] if not focus or s['id'] in focus['supply_ids']]
    return {'goal': state['goal'], 'direction': direction,
            'research_focus': focus,
            'initial_public_query': state.get('initial_public_query'),
            'latest_request': context.get('latest_request', ''),
            'locked_item_ids': context.get('locked_item_ids', []),
            'decisions': previous.get('decisions', []),
            'catalog': [{k: s[k] for k in fields if k in s}
                | {'source_options': [url for url, ids in known_urls.items() if s['id'] in ids]}
                for s in supplies][:65],
            'tool_results': state['results'][-4:], 'open_gaps': state['open_gaps'],
            'source_observations': [{k: e.get(k) for k in ('id', 'source_url', 'supply_ids', 'status', 'query_id')}
                | {'excerpt': e.get('text', '')[:2500], 'untrusted_external_content': True}
                for e in state['evidence'][-8:] if e.get('method') != 'tool_execution'
                and (not focus or set(e.get('supply_ids', [])) & set(focus['supply_ids']))],
            'remaining_rounds': MAX_ROUNDS - state['round'] - 1}


def _accept_response(response, state):
    if not isinstance(response, dict):
        raise ValueError('工作决定必须是对象')
    gaps = response.get('open_gaps', [])
    if isinstance(gaps, list):
        if state.get('research_focus') is None:
            state['open_gaps'] = [{key: _string(gap.get(key)) for key in ('title', 'detail', 'kind')}
                                  | {'owner': 'agent', 'status': 'open'}
                                  for gap in gaps[:8] if isinstance(gap, dict)]
    state['experience_brief'] = _string(response.get('experience_brief'))
    actions = response.get('actions', [])
    if not isinstance(actions, list) or any(not isinstance(a, dict) for a in actions):
        raise ValueError('工作动作必须为对象列表')
    if len(actions) > MAX_ACTIONS:
        state['open_gaps'].append({'title': '还有供给研究未执行',
            'detail': '本轮动作数量超过执行上限，多余动作未执行。', 'kind': 'supply'})
    return actions[:MAX_ACTIONS]


def _run_actions(actions, state, tools, seen, publish):
    for action in actions:
        decision = (initial_public_research.authorize(action, state.get('initial_public_query'))
                    if action.get('tool') == 'search_public' and state.get('research_focus') is None
                    else authorize_research_action(action, state.get('research_focus'), state))
        if not decision['allowed']:
            state['tasks'].append(_blocked(action, decision['gap']['detail'], len(state['tasks'])))
            if decision['gap'] not in state['open_gaps']:
                state['open_gaps'].append(decision['gap'])
            publish(copy.deepcopy(state))
            continue
        action = decision['effective_action']
        if state.get('research_focus') is None:
            action = _untried_source(action, state)
        name = action.get('tool')
        signature = (tools.request_signature(action, state['supplies']) if hasattr(tools, 'request_signature')
                     else json.dumps({k: v for k, v in action.items()
                         if k not in ('reason', 'automatic_source_fallback', 'gap_id', 'domain')}, sort_keys=True))
        if not isinstance(name, str) or name not in TOOLS or signature in seen:
            detail = '相同动作已执行，不重复请求。' if signature in seen else '当前没有这个执行能力，未执行。'
            state['tasks'].append(_blocked(action, detail, len(state['tasks'])))
            publish(copy.deepcopy(state))
            continue
        seen.add(signature)
        try:
            outcome = tools.execute(action, state['supplies'], state['evidence'])
            state['supplies'] = outcome['supplies']
            admit_discovered_candidates(state.get('research_focus'), action, state['supplies'])
            state['evidence'] = outcome['evidence']
            state['capabilities'] = outcome.get('capabilities', [])
            state['tasks'].append(outcome['task'])
            state['results'].append({'action': action, 'task': outcome['task'],
                                     'result': outcome.get('result', {})})
        except (ValueError, KeyError, TypeError, OSError):
            task = _blocked(action, '本次工具未能取得结果，原有资料保留；没有确认或预订供给。', len(state['tasks']))
            state['tasks'].append(task)
            state['results'].append({'action': action, 'task': task, 'result': {}})
        publish(copy.deepcopy(state))


def _untried_source(action, state):
    """A missing URL must not turn 'try another source' into the same failure.

    This is an explicit harness fallback over already discovered provenance,
    not model-generated URL access, page-link crawling or a supplier action.
    """
    if action.get('tool') != 'inspect_source' or action.get('url') is not None:
        return action
    from .capability_tools import _sources
    id_ = action.get('supply_id')
    supply = next((s for s in state['supplies'] if s['id'] == id_), None)
    if not supply:
        return action
    primary = supply.get('product_url')
    primary = primary if isinstance(primary, str) and primary.startswith('https://') else supply.get('source_url')
    receipts = {e.get('source_url'): e.get('status') for e in state['evidence'] if e.get('source_url')}
    if receipts.get(primary) not in ('blocked', 'unavailable'):
        return action
    alternatives = [url for url, ids in _sources([supply]).items() if id_ in ids and url not in receipts]
    if not alternatives:
        return action
    return {**action, 'url': alternatives[0], 'automatic_source_fallback': True}


def organize(direction, context, tools, caller, usage, publish):
    from .phase1_budget_context import current_budget, use_budget
    from .initial_contract import SHARD_THRESHOLD
    budget = current_budget()
    if budget is None or context.get('previous_plan') or budget.level != 'L0':
        return _organize(direction, context, tools, caller, usage, publish)
    days = direction.get('days', 1)
    writer_calls = days + 2 if days >= SHARD_THRESHOLD else 1
    from .initial_budget import DELIVERY_REVIEW_CALLS
    allowance = max(0, budget.snapshot()['calls_remaining'] - writer_calls - DELIVERY_REVIEW_CALLS - 1)
    research_budget = budget.child('L1', 'initial-research', seconds=120, max_calls=allowance)
    def publish_research(state):
        # Failed/expired tool receipts are still useful; only the parent attempt
        # may persist them. Transport leases continue to fence late tool results.
        with use_budget(budget):
            publish(state)
    try:
        with use_budget(research_budget):
            return _organize(direction, context, tools, caller, usage, publish_research)
    finally:
        usage['initial_research_budget'] = research_budget.snapshot()


def _organize(direction, context, tools, caller, usage, publish):
    """Reads compact trip context, writes only a tool-work state delta.

    publish is called after each atomic tool result, before another model call.
    The caller owns final plan validation/commit. No tool output alters orders.
    """
    work = direction.get('work_plan') or {}
    previous = context.get('previous_plan') or {}
    state = {'goal': _string(work.get('goal') or direction.get('premise')),
             'supplies': copy.deepcopy(context.get('previous_supplies', previous.get('supplies', []))),
             'evidence': copy.deepcopy(context.get('previous_evidence', [])),
             'capabilities': copy.deepcopy(previous.get('journey', {}).get('capabilities', [])),
             'tasks': copy.deepcopy(context.get('previous_tool_tasks', [])), 'results': [],
             'open_gaps': [], 'experience_brief': '', 'budget_exhausted': False, 'round': 0}
    focus = compile_research_focus(context.get('research_scope'), state['supplies'], state['evidence'])
    state['research_focus'] = focus
    state['initial_public_query'] = initial_public_research.compile_query(direction) if focus is None and not previous else None
    if focus is not None:
        state['open_gaps'] = copy.deepcopy(focus['gaps']) or [blocked_gap()]
    actions = work.get('actions')
    if not isinstance(actions, list):
        actions = [{'tool': 'search_supply', 'queries': direction.get('research_queries', [])}]
    actions = _accept_response({'actions': actions}, state)
    seen = {e['request_signature'] for e in state['evidence']
            if focus is not None and isinstance(e.get('request_signature'), str) and recent_research(e)}
    if focus is not None and focus['initial_query_id']:
        _run_actions([{'tool': focus['queries'][focus['initial_query_id']]['tool'],
                       'query_id': focus['initial_query_id']}],
                     state, tools, seen, publish)
    for round_ in range(MAX_ROUNDS):
        state['round'] = round_
        _run_actions(actions, state, tools, seen, publish)
        if round_ == 0 and state.get('initial_public_query') and not state['supplies']:
            _run_actions([{'tool': 'search_public', 'query_id': state['initial_public_query']['query_id']}],
                         state, tools, seen, publish)
        if round_ == MAX_ROUNDS - 1:
            state['budget_exhausted'] = True
            state['open_gaps'].append({'title': '本轮研究已到执行边界', 'detail':
                '已取得的资料可用于方案；剩余现实条件尚未全部确认，不能视为旅行准备完成。', 'kind': 'supply',
                'owner': 'agent', 'status': 'open'})
            break
        try:
            response, metrics = caller(atomic_prompts.work_messages(_work_context(direction, context, state)),
                                       max_tokens=3000, reasoning_effort='none')
            add_usage(usage, metrics)
            actions = _accept_response(response, state)
            if not actions:
                break
        except (model.ModelError, ValueError, KeyError, TypeError, TimeoutError) as error:
            add_failed_usage(usage, error)
            usage['work_model_failures'] = usage.get('work_model_failures', 0) + 1
            state['tasks'].append(_blocked({}, '进一步研究未完成；用已有证据形成可调整方案。', len(state['tasks'])))
            publish(copy.deepcopy(state))
            break
    usage['autonomous_tool_count'] = len(state['results'])
    usage['autonomous_rounds'] = state['round'] + 1
    usage['research_budget_exhausted'] = state['budget_exhausted']
    for result in state['results']:
        if result['result'].get('search_usage'):
            add_usage(usage, result['result']['search_usage'])
    if focus is not None:
        usage['research_focus'] = {'supply_ids': focus['supply_ids'],
            'gap_ids': [g['id'] for g in focus['gaps']], 'registered_queries': len(focus['queries'])}
        usage['public_search_queries'] = sum(r['result'].get('search_executed') is True for r in state['results'])
        usage['public_source_reads'] = sum(r['result'].get('source_attempt_count', 0)
            for r in state['results'] if r['action'].get('tool') == 'search_public')
    return state
