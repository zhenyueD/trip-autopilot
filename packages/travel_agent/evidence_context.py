"""Deduplicate planning context without deleting the durable evidence ledger."""
from copy import deepcopy


def planning_evidence(records):
    latest, last_read, bindings = {}, {}, {}
    for record in records:
        url = record.get('source_url') or record.get('url')
        if not url or record.get('method') == 'tool_execution':
            continue
        latest[url] = record
        bindings.setdefault(url, set()).update(record.get('supply_ids', []))
        if record.get('status') == 'read':
            last_read[url] = record
    result = []
    for url, current in latest.items():
        if current.get('status') != 'read' and url in last_read:
            previous = deepcopy(last_read[url])
            previous.update(historical_observation=True, latest_attempt_status=current.get('status'),
                            latest_attempt_id=current.get('id'), supply_ids=sorted(bindings[url]))
            result.append(previous)
        item = deepcopy(current)
        item['supply_ids'] = sorted(bindings[url])
        result.append(item)
    return result
