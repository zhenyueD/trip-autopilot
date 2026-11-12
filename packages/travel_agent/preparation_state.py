"""Conservative preservation of user reports; no model can self-confirm them."""
from copy import deepcopy


def _dependencies(plan, ids):
    return [(day.get('day'), day.get('date'), day.get('lodging'), item)
            for day in (plan or {}).get('days', []) for item in day.get('items', [])
            if item.get('id') in ids]


def preserve_reports(pack, current, previous):
    result = deepcopy(pack)
    old = {a['id']: a for a in ((previous or {}).get('preparation_pack') or {}).get('actions', [])}
    # Research is incremental: focusing on another gap must not erase usable
    # instructions or personal reports whose itinerary dependencies still match.
    for prior in old.values():
        ids = prior.get('related_item_ids', [])
        equivalent = any(a.get('id') == prior['id'] or (
            a.get('category') == prior.get('category') and
            sorted(a.get('related_item_ids', [])) == sorted(ids))
            for a in result.get('actions', []))
        unchanged = (_dependencies(current, ids) == _dependencies(previous, ids) if ids
                     else current.get('decisions') == (previous or {}).get('decisions'))
        if prior.get('status') in ('ready', 'needs_user', 'user_reported') and unchanged and not equivalent:
            result.setdefault('actions', []).append(deepcopy(prior))
            used = set(prior.get('source_ids', []))
            existing = {s.get('id') for s in result.get('sources', [])}
            for source in ((previous or {}).get('preparation_pack') or {}).get('sources', []):
                if source.get('id') in used and source.get('id') not in existing:
                    result.setdefault('sources', []).append(deepcopy(source))
    stable_fields = ('title', 'category', 'day', 'related_item_ids', 'instructions', 'when',
                     'completion_condition', 'action_url', 'materials', 'assumptions')
    for action in result.get('actions', []):
        prior = old.get(action['id'], {})
        if prior.get('status') != 'user_reported' or action.get('status') == 'needs_research':
            continue
        ids = action.get('related_item_ids', [])
        same = all(action.get(k) == prior.get(k) for k in stable_fields)
        same = same and _dependencies(current, ids) == _dependencies(previous, ids)
        if not ids:
            same = same and current.get('decisions') == (previous or {}).get('decisions')
        if same and action.get('owner') == 'user':
            action.update(status='user_reported', reported_at=prior.get('reported_at'),
                          status_before_report=prior.get('status_before_report', 'needs_user'))
        else:
            action['report_note'] = '你曾报告办好；相关安排或步骤已变化，请重新确认适用性。'
    result['status'] = 'partial' if any(a.get('status') == 'needs_research'
                                        for a in result.get('actions', [])) else 'ready'
    return result
