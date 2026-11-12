"""Optional Phase 1 review hook; no publication or changes to Phase 0 criteria."""
import copy
import math
import threading
from uuid import uuid4

from .blind_spots import blind_spot_check, run_blind_spot_critic, repair_blind_spot_review
from .execution_budget import ExecutionBudget, run_with_deadline
from .source_policy_view import source_policy_view
from .validation import PlanError
from .writer_scope import at


# Core caller: 120s plus its 25% repair allowance. Receipt: two 24s calls,
# each with the same allowance. Insufficient review time is a failure, not a skip.
REVISION_REVIEW_RESERVE_SECONDS = 120 * 1.25 + 2 * 24 * 1.25
REVISION_REVIEW_RESERVED_CALLS = 3  # At least one core and two receipt attempts.


def _changed_roots(diff):
    roots = []
    for change in diff:
        parts = change['path'].split('/')[1:]
        if not parts:
            continue
        count = 2 if parts[0] == 'days' and len(parts) > 1 and parts[1].isdigit() else 1
        root = '/plan/' + '/'.join(parts[:count])
        if root not in roots and parts[0] not in ('supplies', 'source_evidence'):
            roots.append(root)
    return roots


def _relevant_nodes(payload):
    roots = _changed_roots(payload['mechanical_diff'])
    supplied = payload.get('candidate_nodes') or {}
    candidate = payload.get('candidate')
    if isinstance(candidate, dict) and any(change['path'] == '' for change in payload['mechanical_diff']):
        roots = ['/plan/' + key for key in candidate if key not in ('days', 'supplies', 'source_evidence')]
        roots += ['/plan/days/' + str(index) for index, _ in enumerate(candidate.get('days', []))]
    nodes = {}
    for root in roots:
        try:
            if isinstance(candidate, dict):
                nodes[root] = copy.deepcopy(at({'plan': candidate}, root))
                continue
            for path, value in supplied.items():
                if root == path or root.startswith(path + '/'):
                    suffix = root[len(path):]
                    nodes[root] = copy.deepcopy(at(value, suffix) if suffix else value)
                    break
                if path.startswith(root + '/'):
                    nodes[path] = copy.deepcopy(value)
        except (PlanError, KeyError, IndexError, TypeError):
            nodes[root] = {'node_absent': True}
    # Retain actual restrictions/availability/locations, not duplicate catalog
    # descriptions or evidence bodies. Every day/item stays intact; source
    # summaries provide admitted URLs for new lookup, and the critic receives
    # the actual resulting excerpts separately.
    if isinstance(nodes.get('/plan/journey'), dict):
        journey = nodes['/plan/journey']
        if isinstance(journey.get('capabilities'), list):
            journey['capabilities'] = [{key: copy.deepcopy(row[key]) for key in
                ('id', 'supply_id', 'kind', 'what', 'constraints', 'location', 'price',
                 'inventory', 'availability', 'booking_status', 'boundary') if key in row}
                | {'acquisition': {key: row['acquisition'][key] for key in
                    ('status', 'mode', 'can_book', 'can_pay', 'can_contact')
                    if key in row.get('acquisition', {})}}
                for row in journey['capabilities'] if isinstance(row, dict)]
    if isinstance(nodes.get('/plan/preparation_pack'), dict):
        pack = nodes['/plan/preparation_pack']
        if isinstance(pack.get('sources'), list):
            pack['sources'] = [{key: copy.deepcopy(row[key]) for key in
                ('id', 'title', 'status', 'reason', 'http_status') if key in row}
                for row in pack['sources'] if isinstance(row, dict)]
    return source_policy_view(nodes)


def _review_context(payload, base):
    nodes = _relevant_nodes(payload)
    context = {key: copy.deepcopy(base[key]) for key in
               ('destination', 'dates', 'source_summaries', 'lookup_capabilities') if key in base}
    context.update(candidate_nodes=nodes, traveler_profile=copy.deepcopy(payload.get('traveler_profile') or {}))
    days = [(path, value) for path, value in nodes.items()
            if path.startswith('/plan/days/') and isinstance(value, dict)]
    context['day_keys'] = list(dict.fromkeys('day-' + str(value.get('day', path.split('/')[3]))
                                            for path, value in days))
    if not context['day_keys']:
        context['day_keys'] = copy.deepcopy(base.get('day_keys', []))
    dates = [value['date'] for _, value in days if value.get('date')]
    # Candidate dates may be model assumptions. In particular, an explicit
    # empty baseline means no actual travel date is known, not permission to
    # promote a generated day date into the review's factual context.
    if dates and 'dates' not in context:
        context['dates'] = dates
    return context


def _call_usage(records):
    model_records = [row for row in records if row['kind'] in
                     ('blind_spot_model', 'blind_spot_critic', 'blind_spot_repair')]
    usage = {'model_calls': len(model_records), 'input_tokens': 0, 'output_tokens': 0,
             'usage_complete': True, 'calls': copy.deepcopy(records)}
    for record in model_records:
        observed = record.get('usage') or {}
        for field in ('input_tokens', 'output_tokens'):
            value = observed.get(field)
            if type(value) is int:
                usage[field] += value
            else:
                usage['usage_complete'] = False
    return usage


def _unavailable_review(payload, budget, reason):
    snapshot = budget.snapshot()
    records = copy.deepcopy(snapshot['calls'])
    for record in records:
        if record['status'] == 'running':
            record.update(status='timeout', reason=reason)
    objections = ['补充风险审查预算不足或已结束，原方案保留；不能将未完成核对视为通过']
    sixth = {'area': 'blind_spots', 'verdict': 'fail', 'candidate_checks': [],
             'objections': objections, 'failure_kind': 'operational', 'failure_reason': reason}
    return {'candidate_sha256': payload['candidate_sha256'], 'diff_sha256': payload['diff_sha256'],
            'verdict': 'fail', 'objections': objections, 'sixth_check': sixth,
            'blind_spots': {'status': 'incomplete', 'candidates': [], 'issues': objections,
                           'semantic_review_required': True},
            'failure_scope': 'supplemental_review', 'review_failure_kind': 'operational',
            'usage': _call_usage(records), 'review_budget': snapshot}


class Phase1RevisionReview:
    """Callable for ``revise_plan(candidate_review=...)``.

    Use a one-attempt raw caller (e.g. ``model.call_json``), not the planner
    wrapper with hidden repair/fallback calls. The caller is charged here.
    ``budget`` is the same acceptance-time L0 used for writer/tools/critic;
    ``context`` contains actual destination/dates and optional stable day_keys.
    Reuse this instance and budget on in-process resume; never reset allowance.
    """
    def __init__(self, *, caller, budget, resolver=None, context=None, known_source_refs=None):
        self.caller, self.budget, self.resolver = caller, budget, resolver
        self.context = copy.deepcopy(context or {})
        self.known_source_refs = known_source_refs
        self._lock = threading.RLock()

    def _revision_budget(self, deadline):
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
            raise PlanError('修订审查必须使用有效的绝对截止时间')
        cutoff = min(deadline, self.budget.deadline) - REVISION_REVIEW_RESERVE_SECONDS
        started = min(self.budget._clock(), cutoff)
        # Keep the L0 level: L1 labels are actual day keys in blind_spot_check.
        # _parent shares ancestor closure, physical-call records, and call caps.
        return ExecutionBudget(self.budget.level, started_at=started,
            seconds=max(0, cutoff - started), _parent=self.budget,
            max_calls=max(0, self.budget.snapshot()['calls_remaining'] - REVISION_REVIEW_RESERVED_CALLS),
            label='revision-review-' + uuid4().hex[:12])

    def __call__(self, payload, *, deadline=None):
        if deadline is None:
            return self._review(payload, self.budget, self.resolver)
        budget = self._revision_budget(deadline)
        reason = budget.snapshot()['reason']
        if reason:
            budget.close('review_allowance_unavailable')
            return _unavailable_review(payload, budget, reason)
        # The production resolver replaces its per-instance source snapshots.
        # Isolate those writes and the candidate before a callback can outlive us.
        isolated, resolver = copy.deepcopy(payload), copy.copy(self.resolver)
        result = run_with_deadline(lambda: self._review(isolated, budget, resolver,
            allow_hypothesis_correction=True), budget)
        if result.status != 'ok':
            return _unavailable_review(payload, budget, result.reason or result.status)
        return result.value

    def _review(self, payload, budget, resolver, *, allow_hypothesis_correction=False):
        with self._lock:
            prior = {row['call_id'] for row in budget.snapshot()['calls']}
            base = copy.deepcopy(self.context)
            if hasattr(resolver, 'refresh'):
                resolver.refresh()
                base['source_summaries'] = copy.deepcopy(resolver.sources)
                base['lookup_capabilities'] = copy.deepcopy(getattr(resolver, 'lookup_capabilities', {}))
            source_snapshot = copy.deepcopy(getattr(resolver, 'source_snapshot', None))
            context = _review_context(payload, base)
            review = blind_spot_check(context, caller=self.caller, budget=budget, resolver=resolver)
            sixth = run_blind_spot_critic(review, caller=self.caller, budget=budget,
                                         known_source_refs=self.known_source_refs)
            repair = None
            if (sixth['verdict'] == 'fail' and not sixth.get('failure_kind')
                    and budget.snapshot()['calls_remaining'] >= 2 and budget.remaining_seconds > 0):
                repair = {'status': 'unavailable_or_invalid',
                    'original_candidates': copy.deepcopy(review['candidates']),
                    'original_sixth_check': copy.deepcopy(sixth)}
                corrected = repair_blind_spot_review(review, sixth, caller=self.caller,
                    budget=budget, resolver=resolver,
                    allow_hypothesis_correction=allow_hypothesis_correction)
                if corrected is not None:
                    review = corrected
                    sixth = run_blind_spot_critic(review, caller=self.caller, budget=budget,
                                                 known_source_refs=self.known_source_refs)
                    repair['status'] = 'reviewed_' + sixth['verdict']
            records = [row for row in budget.snapshot()['calls'] if row['call_id'] not in prior]
            from .phase1_sources import source_bindings, review_refs
            binding = ({'source_bindings': source_bindings(source_snapshot, review_refs({'blind_spots': review}))}
                       if source_snapshot is not None else {})
            return {'candidate_sha256': payload['candidate_sha256'], 'diff_sha256': payload['diff_sha256'],
                    'verdict': sixth['verdict'], 'objections': copy.deepcopy(sixth['objections']),
                    'blind_spots': review, 'sixth_check': sixth, 'usage': _call_usage(records),
                    **({'blind_spot_repair': repair} if repair else {}), **binding,
                    **({'failure_scope': 'supplemental_review',
                        'review_failure_kind': sixth.get('failure_kind') or 'semantic'}
                       if sixth['verdict'] == 'fail' else {})}


def apply_candidate_review(hook, payload, candidate_sha256, diff_sha256, usage, row, *, deadline=None):
    """Isolate a hook and bind its result to the exact validated candidate/diff."""
    bound = copy.deepcopy({**payload, 'candidate_sha256': candidate_sha256, 'diff_sha256': diff_sha256})
    try:
        result = (hook(bound, deadline=deadline) if isinstance(hook, Phase1RevisionReview)
                  else hook(bound))
    except PlanError as error:
        _merge_review_usage(usage, getattr(error, 'usage', None))
        raise
    # Billable calls happened even when the returned review fails its binding
    # or schema check. Account before validation, without accepting its verdict.
    _merge_review_usage(usage, result.get('usage') if isinstance(result, dict) else None)
    if not isinstance(result, dict) or result.get('verdict') not in ('pass', 'fail'):
        raise PlanError('Phase 1补充审查未返回有效结论')
    if result.get('candidate_sha256') != candidate_sha256 or result.get('diff_sha256') != diff_sha256:
        raise PlanError('Phase 1补充审查未绑定当前候选及实际差异')
    objections = result.get('objections')
    if (not isinstance(objections, list) or len(objections) > 8
            or any(not isinstance(item, str) or not item.strip() or len(item) > 1600 for item in objections)
            or bool(objections) != (result['verdict'] == 'fail')):
        raise PlanError('Phase 1补充审查结论与异议不一致')
    sixth = result.get('sixth_check') or {}
    if (not isinstance(sixth, dict) or sixth.get('area') != 'blind_spots'
            or sixth.get('verdict') != result['verdict']):
        raise PlanError('Phase 1缺少一致的第六项语义审查')
    result = copy.deepcopy(result)
    usage.setdefault('phase1_candidate_reviews', []).append(result)
    usage['blind_spots'] = copy.deepcopy(result.get('blind_spots'))
    row['phase1_review'] = result
    return result


def _merge_review_usage(usage, metrics):
    if not isinstance(metrics, dict):
        usage['usage_complete'] = False
        return
    for key in ('model_calls', 'input_tokens', 'output_tokens'):
        if type(metrics.get(key)) is int and metrics[key] >= 0:
            usage[key] = usage.get(key, 0) + metrics[key]
    if metrics.get('usage_complete') is False:
        usage['usage_complete'] = False
    calls = metrics.get('calls')
    if isinstance(calls, list):
        usage.setdefault('calls', []).extend(copy.deepcopy(calls))


def combine_candidate_review(verdict, objections, additional):
    if additional and additional['verdict'] == 'fail':
        return 'fail', list(dict.fromkeys(objections + additional['objections']))
    return verdict, objections
