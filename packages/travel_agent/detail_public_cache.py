"""Short-lived public source snapshots; negative link evidence is never erased."""
from copy import deepcopy
import hashlib
import json
import time

TTL_SECONDS = 900
MAX_ENTRIES = 12


def negative_receipts(cache):
    return deepcopy(cache.get('negative_receipts', [])) if isinstance(cache, dict) else []


def remember_failures(cache, receipts):
    result = deepcopy(cache) if isinstance(cache, dict) else {}
    old = negative_receipts(result)
    chains = deepcopy(result.get('link_chains', []))
    prior = [row for entry in result.get('entries', []) for row in entry.get('receipts', [])]
    for row in prior + list(receipts):
        aliases = [row.get(k) for k in ('url', 'requested_url', 'source_url')]
        aliases.extend(row.get('redirect_chain') or [])
        aliases = list(dict.fromkeys(value for value in aliases if isinstance(value, str)))
        if aliases and aliases not in chains:
            chains.append(aliases)
    for row in receipts:
        code = row.get('http_status')
        if row.get('status') in ('blocked', 'unavailable', 'failed') or (type(code) is int and code != 200):
            if row not in old:
                old.append(deepcopy(row))
    result['negative_receipts'] = old
    result['link_chains'] = chains
    return result


def blocked_urls(cache):
    from .multisource_organize import _reference_key
    rows = negative_receipts(cache)
    for entry in (cache or {}).get('entries', []):
        rows.extend(entry.get('receipts', []))
    chains = [{key for key in map(_reference_key, aliases) if key}
              for aliases in (cache or {}).get('link_chains', [])]
    failed = set()
    for row in rows:
        aliases = [row.get(k) for k in ('url', 'requested_url', 'source_url')]
        aliases.extend(row.get('redirect_chain') or [])
        keys = {key for key in map(_reference_key, aliases) if key}
        chains.append(keys)
        if row in negative_receipts(cache):
            failed.update(keys)
    while True:
        expanded = failed | set().union(*(keys for keys in chains if keys & failed))
        if expanded == failed:
            return failed
        failed = expanded


def key_for(target, languages):
    identity = [target['public_supply'], target['topic'], sorted(set(languages))]
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def live_entries(cache, now=None):
    now = time.time() if now is None else now
    rows = cache.get('entries', []) if isinstance(cache, dict) else []
    return [deepcopy(row) for row in rows if isinstance(row, dict)
            and isinstance(row.get('saved_at'), (int, float))
            and 0 <= now - row['saved_at'] < TTL_SECONDS][-MAX_ENTRIES:]


def cached_sources(cache):
    sources = {}
    for entry in live_entries(cache):
        for row in entry.get('receipts', []):
            if row.get('status') == 'read':
                sources[row['id']] = deepcopy(row)
    return list(sources.values())


def get(cache, target, languages):
    key = key_for(target, languages)
    entry = next((row for row in live_entries(cache) if row.get('key') == key), None)
    if not entry or not any(row.get('status') == 'read' for row in entry.get('receipts', [])):
        return None
    receipts = deepcopy(entry['receipts'])
    for receipt in receipts:
        receipt.update(supply_ids=[target['supply_id']], related_item_ids=deepcopy(target['item_ids']))
    return receipts


def put(cache, target, languages, receipts):
    result = remember_failures(cache, receipts)
    key = key_for(target, languages)
    entries = [row for row in live_entries(result) if row.get('key') != key]
    if any(row.get('status') == 'read' for row in receipts):
        entries.append({'key': key, 'saved_at': time.time(), 'receipts': deepcopy(receipts)})
    result.update(version=1, entries=entries[-MAX_ENTRIES:])
    return result
