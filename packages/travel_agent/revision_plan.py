"""Schema and authority checks for complete writer candidates, never intent rules.

Canonical validators check shape, clocks and references. Their normalized copy is
not used to rewrite untouched content: the critic receives the candidate that
will be committed, including every preserved display field.
"""
import copy
import hashlib
import json

from .material_reader import _validate_url
from .validation import PlanError, _public_supply, records, strings, text, validate_plan


def plan_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def _object(value, label):
    if not isinstance(value, dict):
        raise PlanError(f'{label}必须是对象')
    return value


def _indexed(rows, label):
    result = {}
    for row in records(rows, label, 200):
        id_ = text(row.get('id'), label + 'ID', 160)
        if not id_ or id_ in result:
            raise PlanError(label + 'ID必须非空且唯一')
        result[id_] = row
    return result


def _walk(value, skip_keys=()):
    if isinstance(value, dict):
        yield value
        for key, child in value.items():
            if key not in skip_keys:
                yield from _walk(child, skip_keys)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child, skip_keys)


def _urls(value):
    result = set()
    for row in _walk(value, ('field_evidence', 'evidence_boundary')):
        for key, child in row.items():
            if key in ('url', 'link') or key.endswith('_url'):
                if child not in (None, ''):
                    result.add(text(child, '链接', 8000))
            elif key.endswith('_urls'):
                result.update(strings(child, '链接列表', 100))
    return result


def _trusted_sources(previous, catalog, sources):
    # This is the server's cumulative evidence ledger, not model-authored output.
    # Its growth must not prevent a valid local edit after enough past revisions.
    if not isinstance(sources, list) or any(not isinstance(row, dict) for row in sources):
        raise PlanError('来源必须是对象列表')
    trusted = {row['id']: row for row in sources if row.get('id')}
    for row in (previous.get('preparation_pack') or {}).get('sources', []):
        trusted.setdefault(row['id'], row)
    for row in previous.get('source_evidence', []):
        if row.get('id'):
            trusted.setdefault(row['id'], row)
    for supply in catalog:
        for row in supply.get('source_evidence', []) + supply.get('evidence', []):
            if isinstance(row, dict) and row.get('id'):
                trusted.setdefault(row['id'], row)
    return trusted


def _changed_urls(value, previous):
    if value == previous:
        return set()
    if isinstance(value, dict):
        old = previous if isinstance(previous, dict) else {}
        result = set()
        for key, child in value.items():
            if key in ('field_evidence', 'evidence_boundary'):
                continue
            if key not in old or child != old[key]:
                if key in ('url', 'link') or key.endswith(('_url', '_urls')):
                    result.update(_urls({key: child}))
                else:
                    result.update(_changed_urls(child, old.get(key)))
        return result
    if isinstance(value, list):
        old = previous if isinstance(previous, list) else []
        return set().union(*(_changed_urls(child, old[index] if index < len(old) else None)
                            for index, child in enumerate(value))) if value else set()
    return set()


def _supplies(candidate, previous, catalog, normalized):
    trusted = _indexed(catalog, '可信供给')
    old = {row['id']: row for row in previous.get('supplies', [])}
    selected = _indexed(candidate.get('supplies', normalized['supplies']), '供给')
    for id_, row in selected.items():
        if id_ not in trusted:
            raise PlanError('不存在的供给引用：' + id_)
        selected[id_] = copy.deepcopy(row if row == old.get(id_) else _public_supply(trusted[id_]))
    # Full candidates must retain every referenced supply in their catalog view.
    for row in normalized['supplies']:
        selected.setdefault(row['id'], copy.deepcopy(row))
    candidate['supplies'] = list(selected.values())


def _authority_states(candidate, previous, normalized):
    # These are provenance states, not judgments about the requested edit.
    # Retain existing recorded evidence; new model text is only a hypothesis.
    old_understanding = previous.get('understanding', [])
    for index, row in enumerate(candidate['understanding']):
        if row not in old_understanding:
            row['kind'] = normalized['understanding'][index]['kind']
    old_decisions = previous.get('decisions', [])
    for index, row in enumerate(candidate['decisions']):
        if row not in old_decisions:
            row['status'] = normalized['decisions'][index]['status']
    normalized_items = {item['id']: item for day in normalized['days'] for item in day['items']}
    for day in candidate['days']:
        for item in day['items']:
            if 'action_guide' in item and not isinstance(item['action_guide'], dict):
                item['action_guide'] = copy.deepcopy(normalized_items[item['id']]['action_guide'])


def _reference_checks(candidate, trusted_sources, previous=None):
    items = {item['id'] for day in candidate['days'] for item in day['items']}
    supplies = {row['id'] for row in candidate['supplies']}
    artifacts = {row['id'] for row in (candidate.get('journey') or {}).get('artifacts', [])}
    commitments = set(_indexed(candidate.get('user_commitments', []), '用户要求'))
    anchors = {f"{id_}:d{day['day']}" for id_ in commitments for day in candidate['days']}
    many = {'related_item_ids': items, 'item_ids': items, 'supply_ids': supplies,
            'source_ids': set(trusted_sources), 'evidence_ids': set(trusted_sources)}
    one = {'supply_id': supplies, 'source_id': set(trusted_sources), 'artifact_id': artifacts,
           'item_id': items, 'commitment_id': commitments, 'anchor_id': anchors}
    prior = previous or {}
    old_items = {i['id'] for d in prior.get('days', []) for i in d.get('items', [])}
    old_supplies = {s['id'] for s in prior.get('supplies', [])}
    old_artifacts = {a['id'] for a in (prior.get('journey') or {}).get('artifacts', [])}
    old_commitments = {c['id'] for c in prior.get('user_commitments', [])}
    old_anchors = {f"{id_}:d{day['day']}" for id_ in old_commitments for day in prior.get('days', [])}
    old_known = {**many, **one, 'related_item_ids': old_items, 'item_ids': old_items,
        'item_id': old_items, 'supply_ids': old_supplies, 'supply_id': old_supplies,
        'artifact_id': old_artifacts, 'commitment_id': old_commitments, 'anchor_id': old_anchors}

    def paired(value, old, unchanged_record=True):
        if isinstance(value, dict):
            old = old if isinstance(old, dict) else {}
            if 'id' in value or 'id' in old:
                unchanged_record = unchanged_record and value == old
            yield value, old, unchanged_record
            for key, child in value.items():
                yield from paired(child, old.get(key), unchanged_record)
        elif isinstance(value, list):
            old = old if isinstance(old, list) else []
            for i, child in enumerate(value):
                prior_child = old[i] if i < len(old) else None
                yield from paired(child, prior_child, unchanged_record and child == prior_child)

    def check(values, known, key, row, old, unchanged_record):
        missing = values - known
        # Immutable historical receipts can refer to earlier research catalogs.
        # Preserve only defects already present at this exact position. A ref
        # broken by this revision, or a new/edited/moved record, still fails.
        if missing and not (unchanged_record and row == old and missing.isdisjoint(old_known[key])):
            raise PlanError(key + '含有不存在的引用')
    # Trusted source/catalog records contain their own provenance namespace.
    content = {k: v for k, v in candidate.items() if k not in ('supplies', 'source_evidence')}
    if isinstance(content.get('preparation_pack'), dict):
        content['preparation_pack'] = {k: v for k, v in content['preparation_pack'].items() if k != 'sources'}
    for row, old, unchanged_record in paired(content, prior):
        for key, known in many.items():
            if key in row:
                check(set(strings(row[key], key, 200)), known, key, row, old, unchanged_record)
        for key, known in one.items():
            if key in row and row[key] is not None:
                check({text(row[key], key, 160)}, known, key, row, old, unchanged_record)


def _source_records(candidate, trusted):
    containers = [candidate.get('source_evidence', [])]
    for row in _walk({k: v for k, v in candidate.items() if k != 'supplies'}):
        if 'sources' in row:
            containers.append(row['sources'])
    for rows in containers:
        for id_, source in _indexed(rows, '来源').items():
            if id_ not in trusted:
                raise PlanError('不能创建未经接收的来源：' + id_)
            if any(key not in trusted[id_] or trusted[id_][key] != value for key, value in source.items()):
                raise PlanError('不能改写来源内容或核验状态：' + id_)


def _learning(candidate, previous, trusted):
    old = (previous.get('journey') or {}).get('learning', [])
    for row in records((candidate.get('journey') or {}).get('learning', []), '用户记录', 30):
        if row in old:
            continue
        source = trusted.get(row.get('source'), {})
        quote = row.get('source_quote')
        if (source.get('kind') not in ('user_brief', 'user_text', 'user_feedback')
                or not isinstance(quote, str) or not quote
                or quote not in source.get('content', '') or row.get('text') != source.get('content')):
            raise PlanError('用户记录必须引用实际用户表达，模型不能伪造用户状态')


def _same_authorized_record(record, prior, candidate, previous):
    # A receipt confirms this content, not a reusable ID. Historical notes may
    # annotate it, but cannot relabel a different action as already performed.
    annotations = {'report_note', 'applicability'}
    if ({k: v for k, v in record.items() if k not in annotations}
            != {k: v for k, v in prior.items() if k not in annotations}):
        return False
    ids = set(record.get('related_item_ids', [])) | set(record.get('item_ids', []))
    if record.get('item_id'):
        ids.add(record['item_id'])

    def dependencies(plan):
        return [(day['day'], day.get('date'), day.get('lodging'), item)
                for day in plan.get('days', []) for item in day.get('items', [])
                if item['id'] in ids or (not ids and record.get('day') == day['day'])]

    same = dependencies(candidate) == dependencies(previous)
    # A retained receipt may be explicitly historical, never current proof.
    return same or record.get('applicability') == 'needs_reconfirmation'


def _preparation_pack(candidate, previous):
    if 'preparation_pack' not in candidate:
        return
    pack = _object(candidate['preparation_pack'], 'preparation_pack')
    if 'summary' in pack:
        text(pack['summary'], '准备摘要', 4000)
    if 'status' in pack and pack['status'] not in ('ready', 'partial'):
        raise PlanError('准备包状态不正确')
    old = {a['id']: a for a in (previous.get('preparation_pack') or {}).get('actions', [])}
    day_numbers = {day['day'] for day in candidate['days']}
    for id_, action in _indexed(pack.get('actions'), '准备行动').items():
        text(action.get('title'), '行动标题')
        if action.get('owner') not in ('agent', 'user'):
            raise PlanError('准备行动owner不正确')
        if action.get('status') not in ('ready', 'needs_user', 'needs_research', 'user_reported', 'not_applicable'):
            raise PlanError('准备行动status不正确')
        if action.get('day') is not None and (type(action['day']) is not int or action['day'] not in day_numbers):
            raise PlanError('准备行动引用不存在的日期')
        for key in ('instructions', 'materials', 'assumptions'):
            if key in action:
                strings(action[key], key, 30)
        for key in ('when', 'completion_condition', 'fallback', 'report_note'):
            if key in action:
                text(action[key], key, 4000)
        if action.get('status') == 'user_reported':
            prior = old.get(id_, {})
            if (prior.get('status') != 'user_reported'
                    or not _same_authorized_record(action, prior, candidate, previous)):
                raise PlanError('已办理报告只适用于原内容和关联安排；修改内容须重新确认')


def _journey(candidate, previous):
    if 'journey' not in candidate:
        return
    journey = _object(candidate['journey'], 'journey')
    artifacts = _indexed(journey.get('artifacts', []), '旅行材料')
    for row in artifacts.values():
        for key in ('title', 'content'):
            text(row.get(key), '材料' + key, 4000)
        if row.get('kind') not in ('experience_guide', 'packing_list', 'inquiry_draft', 'route_note'):
            raise PlanError('旅行材料种类不正确')
        if row.get('status', 'generated') != 'generated':
            raise PlanError('生成材料不能声称已执行外部动作')
    old = {r['id']: r for r in (previous.get('journey') or {}).get('tasks', [])}
    for id_, task in _indexed(journey.get('tasks', []), '旅行任务').items():
        text(task.get('title'), '任务标题')
        if not isinstance(task.get('status'), str) or not isinstance(task.get('kind'), str):
            raise PlanError('旅行任务kind和status必须为文字')
        if task.get('status') == 'completed':
            digital = task.get('kind') == 'digital_material' and task.get('artifact_id') in artifacts
            prior_task = old.get(id_, {})
            retained = (prior_task.get('status') == 'completed'
                        and _same_authorized_record(task, prior_task, candidate, previous))
            if not digital and not retained:
                raise PlanError('已完成记录不能转移给修改后的外部任务；须重新确认')
    prior = previous.get('journey') or {}
    if journey.get('physical_fulfillment', 'not_executed') not in ('not_executed', prior.get('physical_fulfillment')):
        raise PlanError('模型不能确认外部履约')


def _adjunct_shapes(candidate, previous):
    for key in ('routes', 'commitment_schedule', 'user_commitments', 'commitment_checks', 'decision_links'):
        if key in candidate:
            records(candidate[key], key, 200)
    for key in ('readiness', 'delivery_truth'):
        if key in candidate:
            _object(candidate[key], key)
    for day in candidate['days']:
        for item in day['items']:
            if 'experience' in item:
                _object(item['experience'], 'experience')
    readiness = candidate.get('readiness') or {}
    if 'ready_to_depart' in readiness and type(readiness['ready_to_depart']) is not bool:
        raise PlanError('ready_to_depart必须是布尔值')
    if readiness.get('ready_to_depart') is True and (previous.get('readiness') or {}).get('ready_to_depart') is not True:
        raise PlanError('模型不能自己确认旅行已准备完毕')
    _preparation_pack(candidate, previous)
    _journey(candidate, previous)


def _links(candidate, previous, catalog, sources, blocked_urls):
    old_urls = _urls(previous)
    trusted_urls = _urls(catalog) | _urls(sources) | old_urls
    blocked = set(blocked_urls)
    for source in sources:
        if source.get('http_status') in (404, 410) or source.get('status') in ('blocked', 'unavailable'):
            blocked.update(_urls(source))
    changed = _changed_urls(candidate, previous)
    for url in _urls(candidate):
        # Preserved historical failure receipts are evidence, not new links.
        # UI safety and the retained unavailable set still suppress them.
        if url not in changed:
            continue
        try:
            _validate_url(url)
        except (ValueError, TypeError, UnicodeError) as error:
            raise PlanError('方案链接必须是安全的公开HTTPS链接') from error
        if url in changed and (url in blocked or url not in trusted_urls):
            raise PlanError('方案引用了未接收或已失效的来源链接')


def _retain_failed_receipts(candidate, previous, sources):
    """Failure receipts are program evidence, not writer-editable display prose.

    Keep them in the reviewed plan so a fresh UI can reconstruct its shared
    unavailable-URL set even if the writer removes the old preparation pack.
    """
    failures = []
    for row in _walk([previous, sources], ('field_evidence', 'evidence_boundary')):
        failed = (row.get('status') in ('blocked', 'unavailable') or
                  type(row.get('http_status')) is int and row['http_status'] != 200)
        provenance = row.get('source_provenance') or {}
        reference = provenance.get('source_ref') if isinstance(provenance, dict) else None
        if failed and (_urls(row) or isinstance(reference, str) and reference.startswith(('http:', 'https:'))):
            record = copy.deepcopy(row)
            record.setdefault('id', 'unavailable-' + plan_sha256(record)[:24])
            failures.append(record)
    if failures:
        evidence = candidate.setdefault('source_evidence', [])
        for record in failures:
            index = next((i for i, row in enumerate(evidence) if row.get('id') == record['id']), None)
            if index is None:
                evidence.append(record)
            else:
                evidence[index] = record


def _locks(candidate, previous, locks):
    old = {i['id']: (day['day'], day.get('date'), i) for day in previous.get('days', [])
           for i in day.get('items', [])}
    new = {i['id']: (day['day'], day.get('date'), i) for day in candidate['days'] for i in day['items']}
    for id_ in locks:
        if id_ not in old or id_ not in new:
            raise PlanError('修改触及锁定安排，需要用户解除锁定')
        if old[id_] != new[id_]:
            # Conservative refusal: a whole-day removal renumbers or redates
            # surviving locked arrangements; the lock semantics are not
            # widened and locks are never auto-remapped.
            if old[id_][2] == new[id_][2]:
                raise PlanError('删除会变动锁定安排的位置/日期，请先处理锁定安排')
            raise PlanError('修改触及锁定安排，需要用户解除锁定')


def validate_revision_plan(raw, previous, catalog, locks=(), *, sources=(), blocked_urls=()):
    """Validate complete JSON; semantic intent/scope/contradictions belong to critic."""
    _object(raw, '完整方案')
    if any(key not in raw for key in ('days', 'preparation', 'summary')):
        raise PlanError('完整方案缺少days、preparation或summary')
    try:
        plan_sha256(raw)
    except (TypeError, ValueError, RecursionError) as error:
        raise PlanError('方案必须是有限的JSON对象') from error
    candidate = copy.deepcopy(raw)
    normalized = validate_plan(candidate, catalog, previous, list(locks))
    _locks(candidate, previous, locks)
    _supplies(candidate, previous, catalog, normalized)
    _authority_states(candidate, previous, normalized)
    _adjunct_shapes(candidate, previous)
    trusted = _trusted_sources(previous, catalog, list(sources))
    _source_records(candidate, trusted)
    _learning(candidate, previous, trusted)
    _reference_checks(candidate, trusted, previous)
    _links(candidate, previous, catalog, list(trusted.values()), blocked_urls)
    _retain_failed_receipts(candidate, previous, list(trusted.values()))
    return candidate
