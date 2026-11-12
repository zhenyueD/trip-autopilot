"""Deterministic structural reuse for shard revisions (incremental re-run).

The program never parses feedback text. The scope model declares whole-day
removals as structured ``removed_days`` (old plan day numbers); the program
only verifies the declaration against structural facts (in range, no lock
conflict, no edit-path conflict, not deleting everything) and then owns the
mechanics: removed days need no draft, unselected untouched kept days keep
their original bytes, and only border days (next to a removed block), the
model-selected days and global sections are re-drafted.
"""
from .writer_scope import _parts

FULL_SCOPE_REASON = '模型未声明整天删除，按模型选定范围起草；局部范围不扩大为全程重写。'

# Declaration validation clauses (numbered for audit references):
# V1 every element is an int (bool excluded); V2 all exist among plan day
# numbers; V3 lock pre-check — removed days' items disjoint from
# locked_item_ids; V4 edit_paths do not conflict with removed day roots;
# V5 at least one day survives.


def _day_index(previous):
    return {day.get('day'): i for i, day in enumerate(previous.get('days', []))
            if isinstance(day.get('day'), int)}


def border_days(previous, valid_set):
    """Old day numbers adjacent to a deleted block — the structural
    dependencies that must be drafted (or, on zero-shard entries, writable)
    even when the model did not select them."""
    deleted = set(valid_set or ())
    if not deleted:
        return []
    days = [d.get('day') for d in previous.get('days', []) if isinstance(d.get('day'), int)]
    return [n for n in days if n not in deleted
            and ((n - 1) in deleted or (n + 1) in deleted)]


def resolve_removal(document, selection, constraints=None):
    """Verify a model removal declaration against structural facts only.

    Returns an audit record: appeared / raw_declared / reason / valid_set.
    A missing or empty declaration is the normal no-removal path; an invalid
    declaration (null, non-array, bool/non-integer element, duplicates, out
    of range, over 100 items, lock conflict, edit-path conflict, deleting
    everything) is rejected as a whole and treated as undeclared — the reason
    is recorded, never silently reinterpreted.
    """
    record = {'appeared': False, 'raw_declared': None, 'reason': None, 'valid_set': []}
    if not isinstance(selection, dict) or 'removed_days' not in selection:
        return record
    record['appeared'] = True
    raw = selection.get('removed_days')
    record['raw_declared'] = raw if isinstance(raw, list) else repr(raw)[:200]
    if raw == []:
        return record
    previous = document.get('plan', document)
    days = previous.get('days', [])
    index = _day_index(previous)

    def reject(reason):
        record.update(reason=reason, valid_set=[])
        return record

    if not isinstance(raw, list) or not raw or len(raw) > 100:
        return reject('removed_days 声明格式无效（须为1..100项天编号数组），整份声明拒绝')
    if any(type(n) is not int for n in raw):  # V1 (bool is not int here)
        return reject('removed_days 含非整数编号，整份声明拒绝')
    if len(set(raw)) != len(raw):
        return reject('removed_days 含重复编号，整份声明拒绝')
    if any(n not in index for n in raw):  # V2
        return reject('removed_days 含行程中不存在的日期编号，整份声明拒绝')
    locked = set((constraints or {}).get('locked_item_ids') or [])
    if locked:  # V3 lock pre-check
        removed_items = {item.get('id') for n in raw for item in days[index[n]].get('items', [])
                         if isinstance(item, dict) and item.get('id')}
        if removed_items & locked:
            return reject('被删日期含锁定安排，整份声明拒绝；请先处理锁定安排')
    indices = {str(index[n]) for n in raw}
    for path in selection.get('edit_paths') or []:  # V4, parsed then intersected
        if not isinstance(path, str):
            continue
        parts = _parts(path)
        if (parts == ['plan'] or parts[:2] == ['plan', 'days'] and
                (len(parts) == 2 or parts[2] in indices)):
            return reject('edit_paths 与被删日期范围冲突（含祖先/后代路径），整份声明拒绝')
    if len(raw) >= len(days):  # V5
        return reject('不能删除全部日期，整份声明拒绝')
    record['valid_set'] = sorted(raw)
    return record


def derive_reuse(previous, valid_set, partitions):
    """Drop shards whose content is deterministically removed or untouched.

    Returns (filtered_partitions, record|None). The affected set is the
    model-selected un-removed edit days ∪ border days ∪ selected global
    sections — a kept day the model explicitly selected is drafted, never
    swallowed by reuse. None means the caller keeps the model-selected
    partition as-is.
    """
    deleted = sorted(set(valid_set or ()))
    if not deleted or not partitions:
        return partitions, None
    days = [d.get('day') for d in previous.get('days', []) if isinstance(d.get('day'), int)]
    index = _day_index(previous)
    kept = [n for n in days if n not in deleted]
    if not kept:
        return partitions, None
    border = border_days(previous, deleted)
    selected_roots = {p.get('root') for p in partitions}
    reused = [n for n in kept if n not in border
              and '/plan/days/' + str(index[n]) not in selected_roots]
    drop_roots = {'/plan/days/' + str(index[n]) for n in deleted + reused}
    filtered = [p for p in partitions if p.get('root') not in drop_roots]
    # Border days are structural dependencies: they are drafted even when the
    # model did not select them.
    have = {p.get('root') for p in filtered}
    missing = ['/plan/days/' + str(index[n]) for n in border
               if '/plan/days/' + str(index[n]) not in have]
    filtered += [{'id': 'fragment-' + str(len(partitions) + i + 1), 'root': root,
                  'status': 'pending', 'attempts': 0, 'edits': [], 'deferred_edits': []}
                 for i, root in enumerate(missing)]
    if not filtered:
        return partitions, None

    def day_hash(number):
        from .writer_critic import plan_hash
        return plan_hash(previous['days'][index[number]])

    basis = '模型声明删除第 ' + '、'.join(str(n) for n in deleted) + ' 天'
    sections = []
    for p in filtered:
        root = p.get('root', '')
        number = None
        if root.startswith('/plan/days/'):
            try:
                number = days[int(root.rsplit('/', 1)[1])]
            except (ValueError, IndexError):
                number = None
        if number in border and '/plan/days/' + str(index[number]) not in selected_roots:
            sections.append({'root': root, 'action': 'redraft', 'reason': '与被删日期相邻，衔接需重算'})
        elif number is not None:
            sections.append({'root': root, 'action': 'redraft', 'reason': '模型选定的编辑日'})
        else:
            sections.append({'root': root, 'action': 'redraft', 'reason': '全局 section，随结构调整'})
    for n in deleted:
        sections.append({'root': '/plan/days/' + str(index[n]), 'action': 'remove', 'reason': basis})
    for n in reused:
        sections.append({'root': '/plan/days/' + str(index[n]), 'action': 'reuse',
                         'reason': '内容未受影响，草稿原文复用'})
    structural_edits = [{'op': 'remove', 'path': '/plan/days/' + str(index[n])}
                        for n in sorted(deleted, key=lambda n: index[n], reverse=True)]
    # whole_day_v1: reuse decisions are whole-day. renumber_map is the day
    # numbering reconciliation must reach; items keep their original bytes.
    ordered_kept = sorted(kept, key=lambda n: index[n])
    record = {'mode': 'incremental', 'basis': basis, 'projection': 'whole_day_v1',
              'deleted_days': deleted, 'border_days': border,
              'reused_days': [{'day': n, 'sha256': day_hash(n),
                               'date': previous['days'][index[n]].get('date'),
                               'city': previous['days'][index[n]].get('city')} for n in reused],
              'renumber_map': {str(n): position + 1 for position, n in enumerate(ordered_kept)},
              'sections': sections, 'structural_edits': structural_edits,
              'reason': (basis + '；未受影响天草稿按原文复用（hash 不变），'
                         '只重算被删/相邻天与全局 section。')}
    return filtered, record


def bind_dependencies(record, context):
    """Bind a reuse decision to the locked inputs it depends on.

    Resume compares this verbatim: reused bytes are only valid for the exact
    locked arrangements and evidence versions the decision was made against.
    """
    constraints = (context or {}).get('request_constraints') or {}
    locked = [str(item) for item in constraints.get('locked_item_ids') or []]
    evidence = []
    for row in (context or {}).get('source_evidence') or []:
        if isinstance(row, dict) and row.get('id') is not None:
            entry = str(row['id'])
            if row.get('version') is not None:
                entry += '@' + str(row['version'])
            evidence.append(entry)
    record['depends_on'] = {'locked_item_ids': sorted(locked), 'source_evidence': sorted(evidence)}
    return record


def full_record(partitions, reason):
    """Audit trail when no verified removal declaration applies.

    mode=full only disables structural reuse; the partition still follows the
    model-selected edit_paths — the local scope is not widened into a full
    rewrite.
    """
    return {'mode': 'full', 'reason': reason, 'projection': 'whole_day_v1', 'renumber_map': {},
            'sections': [{'root': p.get('root', ''), 'action': 'redraft',
                          'reason': '模型选定范围内'} for p in partitions],
            'deleted_days': [], 'border_days': [], 'reused_days': [],
            'structural_edits': []}
