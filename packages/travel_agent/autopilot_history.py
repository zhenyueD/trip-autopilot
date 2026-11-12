"""Project durable exchanges without treating historical facts as current facts."""
import copy
import json

from .autopilot_contract import fingerprint
from .autopilot_workspace_records import material_state, source_freshness


def history_state(trip, snapshot, now):
    """Persist hashes, not another copy of source bodies or user information."""
    sources = {}
    for kind in ('material_sources', 'source_evidence'):
        for index, source in enumerate(trip.get(kind) or []):
            key = kind + ':' + str(source.get('id', index))
            sources[key] = fingerprint([source, source_freshness(source, now)])
    state = {key: value for key, value in trip.items()
             if key not in {'material_sources', 'source_evidence', 'updated_at'}}
    return dict(sources=sources, material_state=material_state(trip, now), context=fingerprint(dict(trip=state,
        goal=snapshot['goal'], user_state=snapshot.get('user_state',
            snapshot['trip'].get('user_state')))))


def compatible(previous, current):
    """Only additions may preserve prior facts; unknown provenance fails closed."""
    return (isinstance(previous, dict) and isinstance(current, dict)
            and previous.get('context') == current.get('context')
            and isinstance(previous.get('sources'), dict)
            and all(current.get('sources', {}).get(key) == value
                    for key, value in previous['sources'].items()))


def project_history(history, snapshot):
    projected = []
    for saved in history:
        content = saved['content']
        native = copy.deepcopy(content['native'])
        prior = content.get('history_state')
        current = compatible(prior, snapshot.get('history_state'))
        if not current:
            if native['role'] == 'assistant':
                native['content'] = 'STALE_HISTORY_CONTEXT: 历史判断与参数已失效，仅保留操作顺序；须以当前资料和用户条件重新判断。'
                if 'reasoning_content' in native:
                    native['reasoning_content'] = ''
                for call in native.get('tool_calls') or []:
                    call['function']['arguments'] = '{}'
            elif native['role'] == 'tool':
                try:
                    receipt = json.loads(native['content'])
                except (ValueError, TypeError):
                    receipt = {}
                if not isinstance(receipt, dict):
                    receipt = {}
                native['content'] = json.dumps(dict(status='invalidated',
                    error_code='STALE_HISTORY_CONTEXT', operation_id=receipt.get('operation_id'),
                    historical_status=receipt.get('status'),
                    already_executed=receipt.get('status') == 'succeeded',
                    result=None, reason='旧事实已失效；操作记录保留，勿重复执行成功操作。'), ensure_ascii=False)
        projected.append(native)
    return projected
