"""Durable bounded drafts for broad revisions; only a full review can publish.

Each fragment owns one disjoint day/domain. Shared edits are proposals for the
reconciliation pass, never partial published changes. A checkpoint is bound to
the request and all factual inputs; successful fragments survive later failures.
"""
import concurrent.futures
import contextlib
import copy
import threading
import time

from .validation import PlanError
from .writer_scope import (EDIT_PROMPT, EDIT_SCHEMA, at, apply_edits,
                           context_bundle, review_context, _parts)
from .writer_scope import normalize_selection, validate_scope_diff, verify_day_structure
from .travel_principles import judgment_context, writer_metadata, attach_metadata
from .writer_context import SCOPED_WRITER_INPUT_LIMIT, bind_context_manifest

SHARED = ('title', 'summary', 'understanding', 'signature', 'change_summary', 'traveler_profile', 'suggestions')
FRAGMENT_PROMPT = '''\n本次是大范围修订中的一个独立草稿分片，shard.root 是本次实际编辑范围。
只落实该日期或领域中的用户要求，其他分片会独立处理。不要重复编写整个行程。
跨日期、准备、摘要联动可以提出精确编辑，但程序只保存为 deferred_edits，最终联动时再核对。
整天的新增或删除会改变日期索引，只能作为待联动建议；分片内原有日期的位置保持稳定。
当前分片确实无需修改时可返回 edits:[],read_paths:[]；不能声称整轮完成。
补读须选具体节点，不能重新索取整个 /plan 或 /plan/days。
'''
RECONCILE_PROMPT = '''\n所有独立分片草稿已合并；这一步核对跨分片依赖并统一准备、摘要和行动项。
nodes 是当前合并草稿，fragment_diffs 为相对原计划的真实变更，deferred_edits 只是待审建议。
只修复必要的联动和 revision_objections，不重新改写已完成或无关的日期。
需要具体全文可用 read_paths 补读。若已一致可以返回 edits:[],read_paths:[]。
最终完整 diff 会交给独立 critic；未通过前不会替换原方案。
'''


class ShardScope(Exception):
    def __init__(self, selection, partitions):
        self.selection, self.partitions = selection, partitions


_NULL_GUARD = contextlib.nullcontext()


def _hash(value):
    from .writer_critic import plan_hash
    return plan_hash(value)


def _within(path, root):
    return isinstance(path, str) and (path == root or path.startswith(root + '/'))


def _owned_edit(edit, root):
    # JSON Patch add/remove at a day root shifts every following day's index.
    # Preserve those requests for reconciliation after stable per-day drafts.
    return (_within(edit.get('path'), root) and not (
        root.startswith('/plan/days/') and edit.get('path') == root
        and edit.get('op') in ('add', 'remove')))


def partition_scope(document, selection):
    """Split model-selected edits, not incidental broad contextual reads."""
    selection = normalize_selection(document, selection)
    paths = selection['edit_paths']
    if not isinstance(paths, list) or len(paths) > 100:
        raise PlanError('edit_paths必须为最多100项路径列表')
    roots = []
    for path in paths:
        if not isinstance(path, str):
            raise PlanError('edit_paths包含无效路径')
        if not _within(path, '/plan'):
            continue
        if path == '/plan':
            roots.extend('/plan/' + key for key in document['plan'])
        elif path.startswith('/plan/days/'):
            root = '/'.join(path.split('/')[:4])
            at(document, root)
            roots.append(root)
        else:
            roots.append('/'.join(path.split('/')[:3]))
    if '/plan/days' in roots:
        roots = [r for r in roots if r != '/plan/days'] + [
            '/plan/days/' + str(i) for i in range(len(document['plan'].get('days', [])))]
    roots = list(dict.fromkeys(r for r in roots if r.split('/')[-1] not in SHARED))
    days = [r for r in roots if r.startswith('/plan/days/')]
    if len(days) < 2 and (days or len(roots) < 2):
        return []
    return [{'id': 'fragment-' + str(i + 1), 'root': root, 'status': 'pending',
             'attempts': 0, 'edits': [], 'deferred_edits': []} for i, root in enumerate(roots)]


def input_hash(context):
    return _hash({key: context[key] for key in ('previous_plan', 'feedback', 'materials',
        'candidates', 'source_evidence', 'pending_materials', 'detail_context', 'request_constraints',
        'traveler_profile', 'travel_principles')})


def resumable_checkpoint(checkpoint, context, request_id):
    if (not isinstance(checkpoint, dict) or checkpoint.get('version') != 1
            or checkpoint.get('request_id') != request_id
            or checkpoint.get('input_sha256') != input_hash(context)
            or checkpoint.get('previous_sha256') != _hash(context['previous_plan'])):
        return False
    try:
        expected = partition_scope({'plan': context['previous_plan']}, checkpoint['selection'])
        from .shard_reuse import (derive_reuse, full_record, bind_dependencies,
                                  resolve_removal, FULL_SCOPE_REASON)
        # Resume re-verifies the declaration against the current baseline and
        # constraints (pure functions, zero model cost): an invalid or tampered
        # declaration degrades to valid_set=[] and the re-derived record then
        # fails the verbatim comparison below, rejecting the resume.
        removal = resolve_removal({'plan': context['previous_plan']}, checkpoint['selection'],
                                  context.get('request_constraints'))
        expected, reuse = derive_reuse(context['previous_plan'], removal['valid_set'], expected)
        if [(s['id'], s['root']) for s in expected] != [(s['id'], s['root']) for s in checkpoint['shards']]:
            return False
        # The reuse audit is part of the durable contract: reused bytes are only
        # valid for the exact decision recorded at draft time. Recompute it and
        # compare verbatim so tampered hashes, dates or dependencies reject resume.
        record = reuse if reuse else full_record(expected, FULL_SCOPE_REASON)
        bind_dependencies(record, context)
        structural_edits = checkpoint.get('structural_edits') or []
        if structural_edits != (record.get('structural_edits') or []):
            return False
        if checkpoint.get('structural_reuse') != {key: value for key, value in record.items()
                                                  if key != 'structural_edits'}:
            return False
        for shard in checkpoint['shards']:
            if shard['status'] == 'accepted':
                if shard.get('fragment_sha256') != _hash(shard['edits']):
                    return False
                if any(not _owned_edit(edit, shard['root']) for edit in shard['edits']):
                    return False
    except (KeyError, TypeError, ValueError, PlanError):
        return False
    return bool(expected)


def _preview(shard):
    pieces = []
    def collect(value):
        if isinstance(value, str) and value.strip():
            pieces.append(value[:240])
        elif isinstance(value, dict):
            for key in ('title', 'task', 'transport', 'time', 'action', 'instruction'):
                if key in value: collect(value[key])
        elif isinstance(value, list):
            for item in value[:3]: collect(item)
    for edit in shard.get('edits', []):
        if edit.get('op') != 'remove': collect(edit.get('value'))
    return list(dict.fromkeys(pieces))[:4]


def progress_summary(checkpoint):
    shards = checkpoint.get('shards', [])
    counts = {key: sum(s['status'] == key for s in shards) for key in ('accepted', 'failed', 'pending')}
    return {'total': len(shards), **counts, 'status': checkpoint.get('status'),
        'can_resume': checkpoint.get('status') != 'reviewed',
        'published': False, 'baseline_preserved': True,
        'fragments': [{'id': s['id'], 'root': s['root'], 'status': s['status'],
                       'review_status': 'awaiting_full_review', 'published': False,
                       'preview': _preview(s)} for s in shards]}


def _apply(candidate, edits):
    return apply_edits(candidate, {'edits': edits, 'read_paths': []}, {}) if edits else copy.deepcopy(candidate)


def _check_reply(raw):
    if (not isinstance(raw, dict) or not {'edits', 'read_paths'} <= set(raw)
            or set(raw) - {'edits', 'read_paths', 'reasoning', 'suggestions'}
            or not isinstance(raw['edits'], list) or not isinstance(raw['read_paths'], list)
            or len(raw['edits']) > 200 or len(raw['read_paths']) > 100
            or (raw['edits'] and raw['read_paths'])):
        raise PlanError('分片writer必须返回互斥的edits/read_paths')
    writer_metadata(raw)


def _write(caller, document, roots, payload, prompt, usage, row, account, deadline, fragment=False,
           lock=None, halted=None, edit_paths=None):
    from .writer_critic import _invoke
    requested = []
    bundle = context_bundle(document, roots, edit_paths or roots)
    nodes = bundle.payload_nodes
    for _ in range(3):
        raw = _invoke(caller, EDIT_PROMPT + prompt, {**payload, 'phase': 'writer',
            'nodes': nodes, 'directory': bundle.directory,
            'context_manifest': bundle.manifest}, usage, row, 'writer', account,
            EDIT_SCHEMA, deadline=deadline, lock=lock, halted=halted,
            context_mode='scoped', max_input_tokens=SCOPED_WRITER_INPUT_LIMIT)
        _check_reply(raw)
        if not raw['read_paths']:
            metadata = writer_metadata(raw)
            # usage/row are shared mutable accounting: metadata writes take the
            # same lock as every other usage mutation in concurrent drafting.
            guard = lock if lock is not None else _NULL_GUARD
            with guard:
                attach_metadata({}, metadata, usage, row)
            return raw['edits'], nodes, metadata
        if fragment and any(p in ('/plan', '/plan/days') for p in raw['read_paths']):
            raise PlanError('分片补读须选择具体日期或领域，不能重复整程读取')
        requested = list(dict.fromkeys(requested + raw['read_paths']))
        bundle = context_bundle(document, list(roots) + requested, edit_paths or roots)
        if bundle.payload_nodes == nodes:
            raise PlanError('分片补读没有新增内容')
        nodes = bundle.payload_nodes
    raise PlanError('分片补读达到上限，尚未产生编辑')


def _fragment(shard, context, state, caller, usage, account, deadline, row=None, lock=None, halted=None):
    guard = lock if lock is not None else _NULL_GUARD
    with guard:
        shard['attempts'] += 1
        if row is None:
            row = {'round': len(usage['writer_critic_rounds']) + 1, 'shard_id': shard['id'], 'verdict': 'pending'}
            usage['writer_critic_rounds'].append(row)
    # Explicit source records may be shared; broad source containers are only
    # indexed. A fragment can ask for precise records without loading all data.
    source_paths = [p for p in state['selection']['read_paths']
                    if not p.startswith('/plan') and len(p.split('/')) >= 3]
    edits, nodes, metadata = _write(caller, state['document'], [shard['root']] + source_paths,
        {'feedback': context['feedback'], 'shard': {'id': shard['id'], 'root': shard['root'], 'stage': 'fragment'},
         **judgment_context(context), 'scope_selection': state['selection'],
         'revision_objections': [shard['error']] if shard.get('error') else []},
        FRAGMENT_PROMPT, usage, row, account, deadline, fragment=True, lock=lock, halted=halted,
        edit_paths=[shard['root']])
    # Cross-root proposals are resolved against the assembled candidate later.
    # A missing dependency field must not discard a valid independent day draft.
    for edit in edits:
        if not isinstance(edit, dict) or edit.get('op') not in ('add', 'replace', 'remove'):
            raise PlanError('分片编辑操作只能add/replace/remove')
        expected = {'op', 'path'} | ({'value'} if edit['op'] != 'remove' else set())
        parts = _parts(edit.get('path'))
        if set(edit) != expected or len(parts) < 2 or parts[0] != 'plan':
            raise PlanError('分片编辑字段不完整或不在方案内')
    own = [e for e in edits if _owned_edit(e, shard['root'])]
    _apply(context['previous_plan'], own)
    with guard:
        shard.update(status='accepted', edits=copy.deepcopy(own),
            deferred_edits=copy.deepcopy([e for e in edits if e not in own]), fragment_sha256=_hash(own),
            read_paths=list(nodes), writer_metadata=metadata)
        shard.pop('error', None)
        row['verdict'] = 'draft_accepted'


def _assembled(previous, shards, structural_edits=()):
    candidate = copy.deepcopy(previous)
    for shard in shards:
        if shard['status'] == 'accepted':
            candidate = _apply(candidate, shard['edits'])
    # Program-owned structural removals (deleted days) apply after shard drafts;
    # indices are pre-sorted descending so earlier removes never shift later ones.
    if structural_edits:
        candidate = _apply(candidate, list(structural_edits))
    return candidate


def _review_all(checkpoint, context, state, caller, validate_candidate, usage, account, save,
                on_progress, deadline, candidate_review=None):
    from .writer_critic import (_invoke, _critic_verdict, mechanical_diff, CRITIC,
                                _checkpoint, RevisionRejected)
    base = _assembled(context['previous_plan'], checkpoint['shards'],
                      checkpoint.get('structural_edits'))
    base_hash = _hash(base)
    checkpoint.update(status='review_pending', assembled_sha256=base_hash)
    save()
    document = {**state['document'], 'plan': base}
    shared = ['/plan/' + key for key in base if key != 'days' and key not in ('supplies', 'source_evidence')]
    # Keep per-domain reads selected by the model; shared display/dependency
    # fields are required for cross-fragment atomic truth, unrelated history is not.
    selected_roots = [s['root'] for s in checkpoint['shards'] if not s['root'].startswith('/plan/days/')]
    shared = [p for p in shared if p in selected_roots or p.split('/')[-1] in SHARED + ('preparation', 'preparation_pack', 'decisions')]
    shared = shared or [checkpoint['shards'][0]['root']]
    objections = checkpoint.get('review_objections', [])
    for number in range(1, 3):
        row = {'round': len(usage['writer_critic_rounds']) + 1, 'shard_id': 'reconciliation', 'verdict': 'pending'}
        usage['writer_critic_rounds'].append(row)
        cached = checkpoint.get('reconciliation') or {}
        if number == 1 and cached.get('input_sha256') == base_hash:
            candidate = _apply(base, cached['edits'])
            attach_metadata(candidate, cached.get('writer_metadata', {}), usage, row)
            if _hash(candidate) != cached.get('candidate_sha256'):
                raise PlanError('联动草稿检查点校验失败')
            bundle = context_bundle(document, shared, state['selection']['edit_paths'])
            nodes = bundle.payload_nodes
        else:
            edits, nodes, metadata = _write(caller, document, shared, {'feedback': context['feedback'],
                **judgment_context(context), 'scope_selection': state['selection'],
                'fragment_judgments': [s.get('writer_metadata', {}) for s in checkpoint['shards']],
                'shard': {'id': 'reconciliation', 'stage': 'reconciliation'},
                'fragment_diffs': mechanical_diff(context['previous_plan'], base),
                'deferred_edits': [e for s in checkpoint['shards'] for e in s['deferred_edits']],
                'revision_objections': objections}, RECONCILE_PROMPT, usage, row, account, deadline,
                edit_paths=state['selection']['edit_paths'])
            candidate = _apply(base, edits)
            attach_metadata(candidate, metadata, usage, row)
            checkpoint['reconciliation'] = {'input_sha256': base_hash, 'edits': edits,
                'writer_metadata': metadata,
                'candidate_sha256': _hash(candidate), 'status': 'draft'}
            save()
        try:
            # Deletion constraint first: the alignment is derived without the
            # declaration, then the scope check compares surviving days'
            # content against the effective edit roots on that same mapping.
            _removal = state.get('removal') or {}
            _valid = _removal.get('valid_set') or ()
            _border = (checkpoint.get('structural_reuse') or {}).get('border_days') or []
            _indices = {d.get('day'): i for i, d in enumerate(context['previous_plan'].get('days', []))}
            _extra = {_indices[n] for n in _border if n in _indices}
            _alignment = verify_day_structure(context['previous_plan'], candidate, _valid)
            validate_scope_diff(context['previous_plan'], candidate, state['selection'],
                                valid_set=_valid, extra_allowed=_extra, alignment=_alignment)
            if 'suggestions' in candidate:
                writer_metadata({'suggestions': candidate['suggestions']})
            validated = validate_candidate(copy.deepcopy(candidate))
            candidate = candidate if validated is None else validated
        except PlanError as error:
            # Repair the merged draft within this same bounded review loop.
            # Preserve accepted fragments, but never resume an invalid merge
            # without returning its concrete validation objection to writer.
            objections = [str(error)[:2400]]
            row.update(verdict='fail', objections=objections)
            checkpoint.update(status='review_failed', review_objections=objections)
            checkpoint.pop('reconciliation', None)
            save()
            continue
        diff = mechanical_diff(context['previous_plan'], candidate)
        row.update(candidate_sha256=_hash(candidate), diff_sha256=_hash(diff))
        _checkpoint(on_progress, {'stage': 'critic', 'status': 'started', 'shard_id': 'final'})
        # Only original baseline nodes go to critic, never a draft presented as
        # the old plan. Diff is computed against the entire original plan.
        critic_bundle = context_bundle(state['document'], shared,
                                        state['selection']['edit_paths'], consumer='critic')
        before_nodes = critic_bundle.payload_nodes
        payload = {'phase': 'critic', 'feedback': context['feedback'],
            'directory': critic_bundle.directory,
            **judgment_context(context), 'scope_selection': state['selection'],
            'writer_reasoning': row.get('writer_reasoning'),
            'fragment_judgments': [s.get('writer_metadata', {}) for s in checkpoint['shards']],
            **review_context(state['document'], candidate, before_nodes, diff), 'mechanical_diff': diff}
        payload['context_manifest'] = bind_context_manifest(
            critic_bundle, payload, consumer='critic')
        additional = None
        if candidate_review is not None:
            from .phase1_revision import apply_candidate_review
            try:
                additional = apply_candidate_review(candidate_review, payload, _hash(candidate), _hash(diff), usage, row,
                                                    deadline=deadline)
            except PlanError as error:
                objections = [str(error)[:1200]]
                row.update(verdict='fail', failure_stage='supplemental_review', objections=objections)
                checkpoint.update(status='review_failed', review_objections=objections)
                save()
                raise RevisionRejected(objections, usage) from None
            save()
        raw = _invoke(caller, CRITIC + '\n这是分片合并后的完整实际diff验收；核对所有日期与准备/摘要的联动。',
            payload, usage, row, 'critic', account, deadline=deadline)
        verdict, objections, checks = _critic_verdict(raw)
        warning_only_failure = verdict == 'pass' and additional is not None and additional['verdict'] == 'fail'
        if additional is not None:
            from .phase1_revision import combine_candidate_review
            verdict, objections = combine_candidate_review(verdict, objections, additional)
        row.update(verdict=verdict, objections=objections, checks=checks)
        if warning_only_failure:
            row['failure_stage'] = 'supplemental_review'
            checkpoint.update(status='review_failed', review_objections=objections)
            save()
            raise RevisionRejected(objections, usage)
        if verdict == 'pass':
            usage['revision_review'] = {'verdict': 'pass', 'previous_sha256': _hash(context['previous_plan']),
                'candidate_sha256': _hash(candidate), 'diff_sha256': _hash(diff), 'checks': checks,
                'round': row['round'], 'limits': '模型语义复核；非独立事实证明或供应确认。'}
            if additional is not None:
                usage['revision_review']['phase1_review'] = copy.deepcopy(additional)
            checkpoint['status'] = 'reviewed'
            save()
            return candidate, usage
        checkpoint.update(status='review_failed', review_objections=objections)
        checkpoint.pop('reconciliation', None)
        save()
    raise RevisionRejected(objections, usage)


def revise_shards(*, context, state, caller, validate_candidate, on_progress, on_usage,
                  on_checkpoint, request_id, deadline, usage, selection=None, partitions=None,
                  checkpoint=None, candidate_review=None, reuse=None):
    from .writer_critic import RevisionRejected, _CheckpointFailed, _CallFailed, _checkpoint
    from .shard_reuse import full_record, bind_dependencies, FULL_SCOPE_REASON
    resumed = checkpoint is not None
    checkpoint = copy.deepcopy(checkpoint) if resumed else {'version': 1, 'request_id': request_id,
        'input_sha256': input_hash(context), 'previous_sha256': _hash(context['previous_plan']),
        'selection': copy.deepcopy(selection), 'shards': copy.deepcopy(partitions), 'status': 'processing'}
    if not resumed:
        # Full-necessity gate audit: which sections re-run, which are reused, why.
        # The decision is bound to the locked inputs it depends on; resume
        # recomputes and compares this record verbatim.
        record = bind_dependencies(reuse or full_record(partitions, FULL_SCOPE_REASON), context)
        checkpoint['structural_edits'] = list(record.get('structural_edits') or [])
        checkpoint['structural_reuse'] = {key: value for key, value in record.items()
                                          if key != 'structural_edits'}
    if resumed:
        usage = copy.deepcopy(checkpoint['usage'])
        usage['resume_count'] = usage.get('resume_count', 0) + 1
    usage['revision_mode'] = 'bounded_shards'
    usage['structural_reuse'] = copy.deepcopy(checkpoint.get('structural_reuse'))
    state['selection'] = checkpoint['selection']
    from .shard_reuse import resolve_removal
    state['removal'] = resolve_removal(state['document'], checkpoint['selection'],
                                       context.get('request_constraints'))
    usage['removal_declaration'] = copy.deepcopy(state['removal'])
    _day_indices = {day.get('day'): i for i, day in enumerate(context['previous_plan'].get('days', []))}
    _border = (checkpoint.get('structural_reuse') or {}).get('border_days') or []
    usage['effective_edit_roots'] = sorted(
        set(checkpoint['selection'].get('edit_paths') or []) |
        {'/plan/days/' + str(_day_indices[n]) for n in _border if n in _day_indices})
    lock = threading.RLock()
    halted = threading.Event()

    def save():
        with lock:
            usage['revision_progress'] = progress_summary(checkpoint)
            checkpoint['usage'] = copy.deepcopy(usage)
            try:
                _checkpoint(on_checkpoint, checkpoint)
                _checkpoint(on_usage, usage)
            except _CheckpointFailed:
                # Durable accounting is unavailable; no new provider call may start.
                halted.set()
                raise

    def account(_):
        save()

    try:
        checkpoint['status'] = 'processing'
        save()
        pending = [s for s in checkpoint['shards'] if s['status'] != 'accepted']
        # Global sections draft against the merged day drafts, never against
        # the stale original: day shards run first; globals then see the
        # assembled intermediate plan. A failed day leaves globals pending
        # for a later resume instead of drafting on inconsistent content.
        day_pending = [s for s in pending if s['root'].startswith('/plan/days/')]
        global_pending = [s for s in pending if not s['root'].startswith('/plan/days/')]
        rows = {}
        for shard in pending:  # Round numbers stay deterministic: assigned in shard order.
            row = {'round': len(usage['writer_critic_rounds']) + 1,
                   'shard_id': shard['id'], 'verdict': 'pending'}
            usage['writer_critic_rounds'].append(row)
            rows[shard['id']] = row

        def draft(shard, phase_state):
            if halted.is_set() or time.monotonic() >= deadline - 1:
                return
            try:
                _checkpoint(on_progress, {'stage': 'writer', 'status': 'started', 'shard_id': shard['id']})
            except _CheckpointFailed:
                # Progress is durable accounting too: when it cannot be
                # persisted, no queued shard may start a provider call.
                halted.set()
                raise
            try:
                _fragment(shard, context, phase_state, caller, usage, account, deadline,
                          row=rows[shard['id']], lock=lock, halted=halted)
            except _CallFailed as error:
                with lock:
                    shard.update(status='failed', error='；'.join(error.objections))
            except (PlanError, TypeError, ValueError, KeyError) as error:
                with lock:
                    shard.update(status='failed', error=str(error)[:1200] if isinstance(error, PlanError)
                                 else '分片输出协议无效')
            if shard['status'] == 'failed':
                with lock:
                    rows[shard['id']].update(verdict='fail', objections=[shard['error']])
            save()

        # Day shards are independent drafts; per-shard writer/critic rounds,
        # budgets and failure semantics are unchanged, only the orchestration
        # is concurrent. A failed shard never aborts the others.
        def run_pool(shards, phase_state):
            workers = min(4, len(shards))
            if workers <= 1:
                for shard in shards:
                    draft(shard, phase_state)
            else:
                with concurrent.futures.ThreadPoolExecutor(
                        max_workers=workers, thread_name_prefix='shard-writer') as pool:
                    for future in concurrent.futures.as_completed(
                            [pool.submit(draft, shard, phase_state) for shard in shards]):
                        future.result()

        run_pool(day_pending, state)
        day_shards = [s for s in checkpoint['shards'] if s['root'].startswith('/plan/days/')]
        if global_pending and all(s['status'] == 'accepted' for s in day_shards):
            intermediate = _assembled(context['previous_plan'], checkpoint['shards'],
                                      checkpoint.get('structural_edits'))
            global_state = {**state, 'document': {**state['document'], 'plan': intermediate}}
            run_pool(global_pending, global_state)
        if any(s['status'] != 'accepted' for s in checkpoint['shards']):
            checkpoint['status'] = 'partial'
            save()
            count = sum(s['status'] == 'accepted' for s in checkpoint['shards'])
            raise RevisionRejected([f'已保存{count}/{len(checkpoint["shards"])}个分片草稿；原方案保留，可继续剩余分片'], usage)
        return _review_all(checkpoint, context, state, caller, validate_candidate, usage,
                           account, save, on_progress, deadline, candidate_review=candidate_review)
    except _CheckpointFailed:
        raise RevisionRejected(['修订状态或用量检查点未保存，停止后续调用'], usage) from None
    except (_CallFailed, PlanError, TypeError, ValueError, KeyError) as error:
        if isinstance(error, RevisionRejected):
            raise
        checkpoint['status'] = 'review_failed'
        try:
            save()
        except _CheckpointFailed:
            raise RevisionRejected(['修订状态或用量检查点未保存，停止后续调用'], usage) from None
        objections = error.objections if isinstance(error, _CallFailed) else [
            str(error)[:1200] if isinstance(error, PlanError) else '分片联动校验未通过']
        raise RevisionRejected(objections, usage) from None
