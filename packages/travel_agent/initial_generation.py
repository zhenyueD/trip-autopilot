"""Resumable first-plan drafts. Only the caller may validate and publish a plan."""
import copy
import time
from concurrent.futures import as_completed

from .initial_contract import (BLUEPRINT_PROMPT, DAY_PROMPT, HANDBOOK_PROMPT, VERSION,
    DAY_SCHEMA, OUTLINE_SCHEMA, HANDBOOK_SCHEMA, day_index, day_payload, schema_prompt,
    stage_schema, blueprint_payload, validate_fragment, validate_outline)
from .model import ModelError
from .phase1_budget_context import ContextThreadPoolExecutor, clamp_deadline
from .revision_plan import plan_sha256
from .runtime import add_failed_usage, add_usage
from .validation import PlanError
from .writer_prompt import writer_messages


def _progress(checkpoint):
    shards = checkpoint.get('shards', [])
    return {'total': len(shards), 'accepted': sum(s['status'] == 'accepted' for s in shards),
        'failed': sum(s['status'] == 'failed' for s in shards), 'status': checkpoint['status'],
        'published': False, 'can_resume': True,
        'fragments': [{'id': s['id'], 'status': s['status'], 'published': False} for s in shards]}


def _call(caller, prompt, payload, schema, deadline, tokens):
    remaining = deadline - time.monotonic()
    if remaining <= 1:
        raise PlanError('本轮时间预算已用完，已验证初建分片可恢复')
    started = time.monotonic()
    schema = stage_schema(schema, payload)
    messages = writer_messages(schema_prompt(prompt, schema), payload)
    try:
        raw, met = caller(messages, max_tokens=tokens, timeout=min(120, remaining / 1.25),
            reasoning_effort='none', model_role='writer', response_schema=schema)
    except Exception as error:
        # Provider error text can contain secrets; only structured accounting is
        # retained. Incomplete JSON is never read, salvaged or checkpointed.
        met = copy.deepcopy(getattr(error, 'usage', {}))
        failure = ('model_truncation' if met.get('finish_reason') == 'length' else
                   'model_call_failed' if not isinstance(error, PlanError) else str(error))
        return None, met, failure, time.monotonic() - started
    return raw, met, None, time.monotonic() - started


def _usable(checkpoint, payload):
    if (checkpoint.get('kind') != 'initial_generation' or checkpoint.get('version') != VERSION
            or checkpoint.get('input_sha256') != plan_sha256(payload)):
        return False
    try:
        blueprint = validate_outline(checkpoint['blueprint'], payload)
        shards = checkpoint['shards']
        if [s['id'] for s in shards] != [f'day-{i}' for i in range(1, len(blueprint['days']) + 1)]:
            return False
        for shard, outline in zip(shards, blueprint['days']):
            if shard['status'] == 'accepted':
                if plan_sha256(shard['result']) != shard.get('result_sha256'):
                    return False
                validate_fragment(shard['result'], outline, payload.get('catalog', []),
                                  payload.get('commitment_anchors', []))
        if checkpoint.get('candidate') is not None:
            if plan_sha256(checkpoint['candidate']) != checkpoint.get('candidate_sha256'):
                return False
    except (PlanError, KeyError, TypeError, ValueError):
        return False
    return True


def compose_initial(*, caller, payload, usage, checkpoint=None, on_checkpoint=None,
                    on_progress=None, autonomous=True):
    """Seven days require nine bounded calls; each accepted unit is durable."""
    checkpoint = copy.deepcopy(checkpoint or {})
    resumed = _usable(checkpoint, payload)
    if not resumed:
        stage = {key: checkpoint[key] for key in
                 ('stage_inputs', 'stage_sha256', 'request_sha256') if key in checkpoint}
        checkpoint = {**stage, 'kind': 'initial_generation', 'version': VERSION,
            'input_sha256': plan_sha256(payload), 'status': 'processing', 'shards': []}
    elif 'model_calls' not in usage:
        usage.update(copy.deepcopy(checkpoint.get('usage', {})))
    usage['initial_generation'] = {'mode': 'bounded_shards', 'resumed': resumed,
        'version': VERSION, 'expected_days': payload['direction']['days']}
    deadline = clamp_deadline(time.monotonic() + 600)

    def save():
        usage['initial_progress'] = _progress(checkpoint)
        checkpoint['usage'] = copy.deepcopy(usage)
        if on_checkpoint:
            on_checkpoint(copy.deepcopy(checkpoint))

    def account(phase, response):
        raw, met, error, elapsed = response
        if error:
            add_failed_usage(usage, ModelError('初建模型调用未完成', met))
        else:
            add_usage(usage, met)
        usage.setdefault('initial_calls', []).append({'phase': phase, **met,
            'elapsed_seconds': round(elapsed, 6), 'status': 'failed' if error else 'ok',
            'failure_code': error, 'model_role': 'writer'})
        return raw, error

    def invoke(phase, prompt, data, schema, tokens):
        raw, error = account(phase, _call(caller, prompt, data, schema, deadline, tokens))
        if error:
            checkpoint.update(status='partial', last_error=error)
            save()
            raise PlanError('初建阶段尚未完成：' + error + '；已有分片已保留，可重试')
        return raw

    save()
    if not resumed:
        try:
            blueprint_data = blueprint_payload(payload)
            for attempt in range(2):
                raw = invoke('blueprint', BLUEPRINT_PROMPT + (
                    '\n仅修正validation_error涉及的字段及必要联动，保留其余路线。' if attempt else ''),
                    blueprint_data, OUTLINE_SCHEMA, 6000)
                try:
                    checkpoint['blueprint'] = validate_outline(raw, payload)
                    usage['initial_reference_gaps'] = [
                        {'day': gap['day'], 'field': gap['field'], 'reason': gap['reason'],
                         'rejected_count': len(gap['rejected_ids'])}
                        for gap in checkpoint['blueprint']['reference_gaps']]
                    break
                except PlanError as invalid:
                    usage['initial_calls'][-1].update(status='rejected',
                        failure_code='blueprint_validation', validation_error=str(invalid))
                    checkpoint['last_error'] = str(invalid)
                    save()
                    if attempt:
                        raise
                    blueprint_data.update(rejected_proposal=raw, validation_error=str(invalid))
        except PlanError as invalid:
            checkpoint.update(status='partial', last_error=str(invalid))
            save()
            raise
        checkpoint.pop('last_error', None)
        checkpoint['shards'] = [{'id': f'day-{i}', 'status': 'pending', 'attempts': 0}
                                for i in range(1, payload['direction']['days'] + 1)]
        save()
    if checkpoint.get('candidate') is not None:
        return copy.deepcopy(checkpoint['candidate'])

    pending = [(shard, outline) for shard, outline in
        zip(checkpoint['shards'], checkpoint['blueprint']['days']) if shard['status'] != 'accepted']
    # Existing executor propagates the single request deadline and physical-call
    # budget into workers. Workers return data only; the coordinator alone saves.
    with ContextThreadPoolExecutor(max_workers=3, thread_name_prefix='initial-day') as pool:
        futures = {}
        for shard, outline in pending:
            shard['attempts'] += 1
            data = day_payload(payload, checkpoint['blueprint'], outline)
            futures[pool.submit(_call, caller, DAY_PROMPT, data, DAY_SCHEMA, deadline, 6500)] = (shard, outline)
        for future in as_completed(futures):
            shard, outline = futures[future]
            raw, error = account(shard['id'], future.result())
            if not error:
                try:
                    result = validate_fragment(raw, outline, payload.get('catalog', []),
                                               payload.get('commitment_anchors', []))
                    shard.update(status='accepted', result=result, result_sha256=plan_sha256(result))
                    shard.pop('error', None)
                except (PlanError, KeyError, TypeError, ValueError) as invalid:
                    error = str(invalid) if isinstance(invalid, PlanError) else '日片结构无效'
            if error:
                shard.update(status='failed', error=error)
                shard.pop('result', None)
                shard.pop('result_sha256', None)
            save()
            if on_progress:
                on_progress(_progress(checkpoint))
    if any(s['status'] != 'accepted' for s in checkpoint['shards']):
        checkpoint['status'] = 'partial'
        save()
        count = _progress(checkpoint)['accepted']
        raise PlanError(f'已保存{count}/{len(checkpoint["shards"])}天完整草稿；仍有日期未完成，可继续剩余分片')

    # All daily content is retained. Only the global writer's input uses an
    # index so seven complete experience essays are not echoed back to it.
    raw = invoke('handbook', HANDBOOK_PROMPT, {**payload, 'initial_phase': 'handbook',
        'blueprint': checkpoint['blueprint'], 'day_index': day_index(checkpoint['shards'])},
        HANDBOOK_SCHEMA, 10000)
    if not isinstance(raw, dict) or 'days' in raw:
        checkpoint.update(status='partial', last_error='handbook_scope')
        save()
        raise PlanError('全局手册必须为对象且不能重新写入days')
    candidate = {**raw, 'days': [copy.deepcopy(s['result']['day']) for s in checkpoint['shards']],
        'user_commitments': copy.deepcopy(payload['direction'].get('user_commitments', [])),
        'commitment_schedule': [copy.deepcopy(row) for s in checkpoint['shards']
                                for row in s['result']['commitment_schedule']]}
    if autonomous and not isinstance(candidate.get('journey'), dict):
        checkpoint.update(status='partial', last_error='handbook_missing')
        save()
        raise PlanError('初建旅行手册缺少journey内容')
    # Core applies the existing full writer validator and its bounded repair
    # loop before commitments, truth and optional critic checks. This is a draft.
    checkpoint.update(status='assembled', candidate=candidate,
                      candidate_sha256=plan_sha256(candidate))
    save()
    return copy.deepcopy(candidate)


def repair_initial(*, caller, payload, candidate, error, usage):
    """Bounded JSON edits preserve accepted daily content during final checks."""
    from .writer_scope import EDIT_PROMPT, EDIT_SCHEMA, apply_edits
    raw, met, failure, _ = _call(caller, EDIT_PROMPT + '''
这是初建完整草稿的最终有界修复。candidate已经包含全部日期与手册，
只用edits修正validation_error及必要联动，路径以/plan开头。不得删天、丢弃手册或改变无关内容。
read_paths必须为空；这里已提供完整候选和输入。不得把条件或建议升级为外部已完成事实。
''', {**payload, 'initial_phase': 'repair', 'candidate': candidate,
        'validation_error': error}, EDIT_SCHEMA, clamp_deadline(time.monotonic() + 150), 6500)
    if failure:
        add_failed_usage(usage, ModelError('初建修复调用未完成', met))
        raise PlanError('初建合并校验修复未完成，已验证日片保留')
    add_usage(usage, met)
    if not isinstance(raw, dict) or raw.get('read_paths'):
        raise PlanError('初建修复必须返回有界edits，不能请求整个方案补读')
    return apply_edits(candidate, raw, {})
