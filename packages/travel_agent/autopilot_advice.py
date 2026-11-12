"""A bounded advice candidate in the existing operation and verifier protocol."""
import copy
import hashlib
import json
import math

from .autopilot_contract import fingerprint
from .autopilot_verifier import _choice, _text, evaluate_criteria
from .store import StateError


ADVICE_VERIFIER_PROMPT = """
本次 candidate.kind=advice：只核验单条建议，不判断整趟 goal 是否完成，不改 goal 的成功条件。
精确建议在 candidate.advice_evidence_id 的 content；声明、来源和未知在 candidate.advice。
建议文字是模型候选，只能证明文字写了什么，不能证明其中外部事实。所有声明均待审。
核对整段文字是否与目标相关，声明清单是否覆盖全部实质断言，原文是否支持、条件与未知是否充分披露。
不得因建议自称“已核实”就判通过；遗漏的实质断言、模糊免责或与未知矛盾的确定语气不得通过。
可以通过明确保留条件和缺证的候选建议，但底层事实保持未知。unknown/conditional 声明不升级为已确认。
保留完整来源中的非官方声明、截断/未读状态、来源语言、同源/转载关系与未评估适用性。
中文与英文只是语言，不是独立来源；两个页面不自动成为两份独立证据。
距离必须对齐测量/路线种类；未说明方式的距离不能当步行距离。跨语言名称相似不证明实体同一。
建议仅以条件形式使用尚未解决的事实时，recommendation.satisfied 可以为true；
对应 claims.satisfied 仍应为null，明确反例/缺口。若建议把这些未知写成确定事实则不得通过。
输出 JSON {"recommendation":<上述单项 criterion 对象>,"claims":[<每个声明的单项 criterion 对象>]}。
recommendation 的 criterion_index 固定0、claim_kind为advice，逐字引用建议证据，
判断整段建议准确、适度、披露未知且没有遗漏实质断言；无法判断则 satisfied=null。
claims 每个对象追加 claim_id，保持原声明ID且每条恰好一次，criterion_index固定0；
claim_kind 按真实事实类型填写（通常external_fact），引用原始来源，不能用建议文字证明外部事实。
每个对象继续包含 reason/counterexample/evidence_ids/supports/independence，格式同原复核协议。
"""

CRITERION_SCHEMA = dict(criterion_index=0, satisfied='true | false | null',
    claim_kind='advice (recommendation only) | plan | user_statement | external_fact | tool_result | external_action',
    reason='nonempty string', counterexample='null | nonempty string', evidence_ids=['source ID'],
    supports=[dict(evidence_id='same source ID', quote='exact original quote',
        supports_verdict='true | false | null', reason='nonempty string',
        applicability=dict(entity='applies | not_applicable | unknown | mismatch',
            time='applies | not_applicable | unknown | mismatch',
            conditions='applies | not_applicable | unknown | mismatch', reason='nonempty string'))],
    independence=dict(required='boolean',
        status='single_source | independent | same_origin | unknown | not_applicable', reason='nonempty string'))
ADVICE_VERIFIER_PROMPT += ('\n完整单项结构（unknown也必须填写全部字段；claims每项另加原claim_id）：'
    + json.dumps(CRITERION_SCHEMA, ensure_ascii=False) + '\n不得省略任何声明或用criteria代替claims。\n')


def make_advice(arguments, operation_id):
    """Hashes bind exact UTF-8 text and all declared claims/unknowns separately."""
    advice = copy.deepcopy(arguments)
    claims, sources = advice['claims'], advice['source_ids']
    ids = [claim['claim_id'] for claim in claims]
    if (not claims or len(set(ids)) != len(ids) or len(set(sources)) != len(sources)
            or any(set(claim['source_ids']) - set(sources) for claim in claims)):
        raise StateError('INVALID_ADVICE', '建议声明或来源标识无效', 400)
    return dict(advice, advice_id='advice-' + operation_id,
                text_sha256=hashlib.sha256(advice['text'].encode()).hexdigest(),
                candidate_sha256=fingerprint(advice))


def advice_integrity(operation):
    """Never accept a changed argument, result text or receipt as the old candidate."""
    result = operation.get('result') or {}
    arguments = operation.get('arguments') or {}
    try:
        expected = make_advice(arguments['advice'], operation['id'])
    except (KeyError, TypeError, AttributeError, StateError):
        return False
    return (operation.get('name') == 'check_constraints'
        and operation.get('request_hash') == fingerprint(dict(name='check_constraints', arguments=arguments))
        and result.get('scope') == 'advice' and result.get('advice') == expected
        and result.get('advice_id') == expected['advice_id']
        and result.get('text_sha256') == expected['text_sha256']
        and result.get('candidate_sha256') == expected['candidate_sha256'])


def advice_matches_state(result, run, trip):
    return (result.get('goal_id') == run['goal_id']
        and result.get('goal_version') == run['goal_version']
        and result.get('trip_revision') == trip['revision']
        and result.get('current_plan_sha256') == fingerprint(trip.get('plan')))


def advice_current(result, run, trip, now):
    until = result.get('valid_until')
    return (advice_matches_state(result, run, trip)
        and type(until) in (int, float) and math.isfinite(until) and until > now)


def _checked(row, criterion, resolve):
    semantic, satisfied = evaluate_criteria({'criteria': [row]}, [criterion], resolve)
    checked = (semantic.get('criteria') or [dict(satisfied=None,
        evidence_checks={'status': 'unresolved', 'gaps': [semantic.get('reason')]})])[0]
    return checked, satisfied, semantic.get('evidence_bindings', {})


def _criterion_issues(row, path, source_ids=None):
    """Only protocol differences, never fill omitted critic judgments."""
    issues = []
    def require(ok, suffix, expected):
        if not ok:
            issues.append(dict(path=path + suffix, code='missing_or_malformed', expected=expected))
    if not isinstance(row, dict):
        return [dict(path=path, code='missing_or_malformed', expected='criterion object')]
    require(type(row.get('criterion_index')) is int and row['criterion_index'] == 0, '.criterion_index', '0')
    kinds = {'advice'} if path == 'recommendation' else {'plan', 'user_statement', 'external_fact', 'tool_result', 'external_action'}
    require(_choice(row.get('claim_kind'), kinds), '.claim_kind', ' | '.join(sorted(kinds)))
    require(_text(row.get('reason')), '.reason', 'nonempty string')
    require('counterexample' in row and (row['counterexample'] is None or _text(row['counterexample'])),
            '.counterexample', 'null | nonempty string')
    require('satisfied' in row and (row['satisfied'] is None or type(row['satisfied']) is bool),
            '.satisfied', 'true | false | null')
    independence = row.get('independence')
    require(isinstance(independence, dict), '.independence', 'object with required/status/reason')
    if isinstance(independence, dict):
        require(type(independence.get('required')) is bool, '.independence.required', 'boolean')
        require(_text(independence.get('reason')), '.independence.reason', 'nonempty string')
        require(_choice(independence.get('status'), {'single_source', 'independent', 'same_origin', 'unknown', 'not_applicable'}),
                '.independence.status', CRITERION_SCHEMA['independence']['status'])
    ids, supports = row.get('evidence_ids'), row.get('supports')
    ids_valid = isinstance(ids, list) and len(ids) <= 20 and all(_text(id_) for id_ in ids)
    require(ids_valid, '.evidence_ids', 'array of at most 20 nonempty source IDs')
    require(isinstance(supports, list) and len(supports) <= 20, '.supports', 'array of at most 20 support objects')
    if not isinstance(supports, list) or len(supports) > 20:
        return issues
    for index, support in enumerate(supports):
        prefix = '.supports[' + str(index) + ']'
        require(isinstance(support, dict), prefix, 'support object')
        if not isinstance(support, dict):
            continue
        for key in ('evidence_id', 'quote', 'reason'):
            require(_text(support.get(key)), prefix + '.' + key, 'nonempty string')
        require('supports_verdict' in support and (support['supports_verdict'] is None or type(support['supports_verdict']) is bool),
                prefix + '.supports_verdict', 'true | false | null')
        app = support.get('applicability')
        require(isinstance(app, dict), prefix + '.applicability', 'object with entity/time/conditions/reason')
        if isinstance(app, dict):
            for key in ('entity', 'time', 'conditions'):
                require(_choice(app.get(key), {'applies', 'not_applicable', 'unknown', 'mismatch'}),
                        prefix + '.applicability.' + key, CRITERION_SCHEMA['supports'][0]['applicability'][key])
            require(_text(app.get('reason')), prefix + '.applicability.reason', 'nonempty string')
    if ids_valid:
        support_ids = {s['evidence_id'] for s in supports if isinstance(s, dict) and _text(s.get('evidence_id'))}
        require(set(ids) == support_ids, '.supports', 'support IDs exactly match evidence_ids')
        if source_ids is not None:
            require(not set(ids) - source_ids, '.evidence_ids', 'only this claim source_ids')
            require(bool(ids) or (not source_ids and row.get('satisfied') is None and _text(row.get('counterexample'))),
                    '.evidence_ids', 'cited source IDs, or explicit unknown with counterexample when no sources exist')
    return issues


def _contract_feedback(raw, advice):
    rows = raw.get('claims') if isinstance(raw, dict) else None
    expected = [claim['claim_id'] for claim in advice['claims']]
    ids = [row['claim_id'] for row in rows if isinstance(row, dict) and _text(row.get('claim_id'))] if isinstance(rows, list) else []
    missing, unexpected = [id_ for id_ in expected if id_ not in ids], sorted(set(ids) - set(expected))
    duplicate = sorted({id_ for id_ in ids if ids.count(id_) > 1})
    issues = _criterion_issues(raw.get('recommendation') if isinstance(raw, dict) else None, 'recommendation')
    if not isinstance(rows, list) or len(rows) != len(expected) or missing or unexpected or duplicate:
        issues.append(dict(path='claims', code='incomplete_claims', expected='exactly one row per expected_claim_id'))
    by_id = {claim['claim_id']: claim for claim in advice['claims']}
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        path = 'claims[' + str(index) + ']'
        if not isinstance(row, dict) or not _text(row.get('claim_id')):
            issues.append(dict(path=path + '.claim_id', code='missing_or_malformed', expected='original claim ID'))
        declaration = by_id.get(row.get('claim_id')) if isinstance(row, dict) and isinstance(row.get('claim_id'), str) else None
        issues += _criterion_issues(row, path, set(declaration['source_ids']) if declaration else None)
    if issues:
        return dict(kind='critic_contract_invalid', next_action='revise_candidate_or_evidence', issues=issues,
            expected_claim_ids=expected, missing_claim_ids=missing, unexpected_claim_ids=unexpected,
            duplicate_claim_ids=duplicate, required_schema=dict(criterion=copy.deepcopy(CRITERION_SCHEMA), claim_id='original claim ID'))


def _nonpass_feedback(recommendation, claims):
    issues = []
    rows = [('recommendation', recommendation)] + [('claims.' + row['claim_id'], row)
        for row in claims if row['declared_status'] == 'asserted']
    for path, row in rows:
        if row.get('satisfied') is not True:
            issues.append(dict(path=path, satisfied=row.get('satisfied'), reason=row.get('reason'),
                counterexample=row.get('counterexample'), gaps=row.get('evidence_checks', {}).get('gaps', [])))
    return dict(kind='advice_not_verified', issues=issues, next_action='revise_candidate_or_evidence')


def _criterion_view(row):
    """Only the declared assessment fields enter public/tool projections."""
    result = {key: copy.deepcopy(row[key]) for key in CRITERION_SCHEMA}
    result['independence'] = {key: result['independence'][key] for key in CRITERION_SCHEMA['independence']}
    result['supports'] = [{key: copy.deepcopy(support[key]) for key in CRITERION_SCHEMA['supports'][0]}
                          for support in row['supports']]
    for support in result['supports']:
        support['applicability'] = {key: support['applicability'][key]
            for key in CRITERION_SCHEMA['supports'][0]['applicability']}
    return result


def evaluate_advice(raw, advice, resolve):
    """Recommendation acceptance and underlying fact status are different values."""
    feedback = _contract_feedback(raw, advice)
    if feedback:
        return dict(status='not_evaluated', reason='incomplete_claims', claims=[], feedback=feedback), 'unknown'
    rows = raw['claims']
    claims = advice['claims']
    recommendation, accepted, bindings = _checked(_criterion_view(raw['recommendation']), advice['text'], resolve)
    if recommendation.get('claim_kind') != 'advice':
        accepted = None
    checked, asserted = [], []
    by_id = {row['claim_id']: row for row in rows}
    for claim in claims:
        # A declaration cannot borrow an unlisted source or the candidate's own text.
        allowed = set(claim['source_ids'])
        row, supported, evidence = _checked(_criterion_view(by_id[claim['claim_id']]), claim['text'],
            lambda id_: resolve(id_) if id_ in allowed else None)
        bindings.update(evidence)
        declaration = claim['status']
        fact = supported if declaration == 'asserted' else None
        status = 'confirmed' if fact is True else 'refuted' if fact is False else 'unknown'
        checked.append(dict(row, claim_id=claim['claim_id'], declared_status=declaration,
                            status=status, satisfied=fact, fact_satisfied=fact))
        if declaration == 'asserted':
            asserted.append(supported)
    verdict = 'pass' if accepted is True and all(value is True for value in asserted) else 'unknown'
    if accepted is False or any(value is False for value in asserted):
        verdict = 'fail'
    semantic = dict(status='evaluated' if verdict != 'unknown' else 'not_evaluated',
        recommendation=recommendation, claims=checked, evidence_bindings=bindings)
    if verdict != 'pass':
        semantic['feedback'] = _nonpass_feedback(recommendation, checked)
    return semantic, verdict
