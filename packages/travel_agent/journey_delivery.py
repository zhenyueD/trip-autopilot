"""Present real work and generated materials separately from physical fulfillment."""
import copy
import hashlib

from .journey_state import plan_changes
from .validation import PlanError, records, strings, text

ARTIFACT_KINDS = {'experience_guide', 'packing_list', 'inquiry_draft', 'route_note'}


def _artifacts(raw, previous, plan):
    journey = raw.get('journey') or {}
    if not isinstance(journey, dict):
        raise PlanError('journey必须为对象')
    old = (previous or {}).get('journey', {}).get('artifacts', [])
    known = {item['id'] for day in plan['days'] for item in day['items']}
    changes = set(plan_changes(previous or {}, plan)['changed_item_ids'])
    # Reuse only artifacts whose related arrangements still exist. The model is
    # instructed to replace stale materials; this also removes dangling links.
    values = journey.get('artifacts', [a for a in old if set(a.get('related_item_ids', [])) <= known
                                     and not (set(a.get('related_item_ids', [])) & changes)])
    result, seen = [], set()
    for artifact in records(values, '旅行材料', 5):
        id_ = text(artifact.get('id'), '材料ID', 80)
        if not id_ or id_ in seen or artifact.get('kind') not in ARTIFACT_KINDS:
            raise PlanError('材料ID必须唯一，材料种类必须是数字内容')
        refs = strings(artifact.get('related_item_ids', []), '关联安排', 30)
        if any(ref not in known for ref in refs):
            raise PlanError('旅行材料引用不存在的安排')
        content = text(artifact.get('content'), '材料内容', 4000)
        if not content.strip():
            raise PlanError('旅行材料必须有实际内容，不能把空材料标为已生成')
        seen.add(id_)
        result.append({'id': id_, 'title': text(artifact.get('title'), '材料标题', 120),
                       'kind': artifact['kind'], 'content': content,
                       'related_item_ids': refs, 'status': 'generated',
                       'limits': 'AI准备的可编辑内容；不是预订确认，也没有发送给供应商。'})
    return result


def _learning(direction, previous, materials, current_ids):
    prior = copy.deepcopy((previous or {}).get('journey', {}).get('learning', []))
    sources = {s['id']: s for s in materials if s.get('id') in current_ids
               and s.get('kind') in ('user_brief', 'user_text', 'user_feedback')}
    updates = direction.get('learning_updates', [])
    for value in updates[:8] if isinstance(updates, list) else []:
        if not isinstance(value, dict):
            continue
        source, quote = sources.get(value.get('source_id')), value.get('quote')
        if not source or not isinstance(quote, str) or not quote.strip() or quote not in source.get('content', ''):
            continue
        # Store the user's actual expression, not a model paraphrase promoted
        # to an identity trait. Newer corrections take precedence in prompts.
        full_expression = source.get('content', '')
        if len(full_expression) > 4000:
            continue  # Keep long material in its ledger, never crop its meaning.
        item = {'text': full_expression, 'source_quote': quote, 'scope': 'this_trip', 'source': source['id'],
                'status': 'user_stated', 'revision': source.get('revision')}
        if not any(x.get('text') == item['text'] and x.get('source') == item['source'] for x in prior):
            prior.append(item)
    return prior[-20:]


def attach_journey(plan, raw, previous, direction, work, context):
    artifacts = _artifacts(raw, previous, plan)
    tasks = copy.deepcopy(work.get('tasks', []))
    for artifact in artifacts:
        tasks.append({'id': 'artifact-' + artifact['id'], 'title': artifact['title'],
            'kind': 'digital_material', 'status': 'completed', 'detail': '内容已生成，可展开使用；未发生外部预订或发送。',
            'supply_ids': [], 'evidence_ids': [], 'artifact_id': artifact['id']})
    for gap in work.get('open_gaps', []):
        tasks.append({'id': 'gap-' + hashlib.sha256(str(gap).encode()).hexdigest()[:12],
            'title': gap.get('title', '尚未准备'), 'kind': 'capability_gap',
            'status': 'needs_confirmation' if gap.get('kind') == 'choice' else 'blocked',
            'detail': gap.get('detail', ''), 'supply_ids': [], 'evidence_ids': []})
    for index, pending in enumerate(plan.get('preparation', [])):
        if pending['status'] == 'not_applicable':
            continue
        tasks.append({'id': f'preparation-{index}', 'title': pending['task'], 'kind': 'preparation',
            'status': 'needs_confirmation' if pending['status'] == 'user_action' else 'blocked',
            'detail': pending['why'], 'supply_ids': pending['supply_ids'], 'evidence_ids': []})
    plan['journey'] = {'version': 1, 'goal': work.get('goal') or direction['premise'],
        'assumptions': direction['assumptions'], 'tasks': tasks, 'artifacts': artifacts,
        'changes': (plan_changes(previous, plan, context.get('latest_request', '')) if previous else
                    {'changed_item_ids': [], 'preserved_item_ids': [], 'changed_decision_ids': [], 'reason': ''}),
        'capabilities': work.get('capabilities', []),
        'learning': _learning(direction, previous, context.get('materials', []), context.get('current_source_ids', [])),
        'work_status': 'proposal_with_gaps', 'physical_fulfillment': 'not_executed'}
    return plan
