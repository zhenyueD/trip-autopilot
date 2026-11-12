"""Trusted adapter to existing writers/validators/critic; never publishes."""
import copy
import time
from types import SimpleNamespace

from . import core_prompts, model, writer_critic
from .core_runtime import direction_view
from .autopilot_workspace_records import RecordMethods, source_freshness
from .autopilot_action_touches import plan_touches
from .initial_contract import SHARD_THRESHOLD
from .initial_generation import compose_initial
from .phase1_budget_context import bind_budget, clamp_deadline, current_budget
from .revision_plan import plan_sha256, validate_revision_plan
from .runtime import add_usage
from .store import StateError
from .validation import PlanError, validate_plan
from .writer_schema import WRITER_PLAN_SCHEMA


def default_context(trip, request, *, clock=time.time):
    previous = trip.get('plan') or {}
    visibility = SimpleNamespace(clock=clock)
    raw_materials = trip.get('material_sources') or []
    raw_evidence = trip.get('source_evidence') or []
    def current(rows):
        return [dict(copy.deepcopy(row), **(source_freshness(row, clock())
                if row.get('valid_until') is not None else {})) for row in rows if isinstance(row, dict)
                and RecordMethods._visible(visibility, row)]
    materials, evidence = current(raw_materials), current(raw_evidence)
    known = {row.get('id') for row in materials + evidence if row.get('id')}
    pending = [row for row in current(trip.get('pending_materials') or [])
               if row.get('source_id') in known
               and row.get('status') not in ('invalidated', 'cancelled')]
    filtered = len(materials) != len(raw_materials) or len(evidence) != len(raw_evidence)
    return dict(materials=materials, evidence=evidence,
                candidates=copy.deepcopy(previous.get('supplies') or []),
                pending_materials=pending,
                detail_context={}, impact_hint={'advisory_only': True},
                direction=None if filtered else copy.deepcopy(trip.get('core_direction')))


class TrustedCandidateBuilder:
    def __init__(self, *, caller=None, context_builder=None, clock=time.time,
                 ttl_seconds=300, on_checkpoint=None):
        self.caller = caller or model.call_planner_json
        self.context_builder = context_builder or (
            lambda trip, request: default_context(trip, request, clock=self.clock))
        self.clock, self.ttl = clock, min(300, max(1, ttl_seconds))
        self.on_checkpoint = on_checkpoint

    def __call__(self, trip, request):
        if (not isinstance(request, dict) or not {'instruction'} <= set(request) <= {'instruction', 'day_ids', 'source_ids'}
                or not isinstance(request['instruction'], str) or not request['instruction'].strip()
                or len(request['instruction']) > 10000):
            raise StateError('INVALID_ARGUMENT', 'request requires instruction and optional day_ids/source_ids', 400)
        for key in ('day_ids', 'source_ids'):
            values = request.get(key, [])
            if (not isinstance(values, list) or len(values) > 100
                    or any((type(v) is not int or v < 1) if key == 'day_ids'
                           else (not isinstance(v, str) or not v) for v in values)):
                raise StateError('INVALID_ARGUMENT', 'Invalid selection', 400)
        if trip.get('workspace_stale_plan_hash') == plan_sha256(trip.get('plan')):
            raise StateError('SOURCE_RECHECK_REQUIRED',
                'Current plan depends on changed sources; trusted recheck or user rebuild required', 409)
        trip, request = copy.deepcopy(trip), copy.deepcopy(request)
        context = self.context_builder(copy.deepcopy(trip), copy.deepcopy(request))
        if not isinstance(context, dict):
            raise StateError('INVALID_CONTEXT', 'Trusted context builder must return object', 500)
        known = {row.get('id') for row in context['materials'] + context['evidence']}
        if any(id_ not in known for id_ in request.get('source_ids', [])):
            raise StateError('SOURCE_UNAVAILABLE', 'Selected source not in trusted context', 409)
        hint = copy.deepcopy(context.get('impact_hint', {'advisory_only': True}))
        if request.get('day_ids'):
            hint['day_numbers'] = request['day_ids']
        if request.get('source_ids'):
            hint['source_ids'] = request['source_ids']
        active = current_budget()
        caller = bind_budget(self.caller, active) if active is not None else self.caller
        deadline = clamp_deadline(time.monotonic() + 480)
        previous = trip.get('plan') or {}
        validate = lambda raw: validate_revision_plan(raw, previous, context['candidates'],
            trip.get('locked_item_ids', []), sources=context['materials'] + context['evidence'])
        if previous:
            plan, usage = writer_critic.revise_plan(previous=previous, feedback=request['instruction'],
                materials=context['materials'], candidates=context['candidates'], evidence=context['evidence'],
                impact_hint=hint,
                pending_materials=context.get('pending_materials', []), caller=caller,
                validate_candidate=validate, scoped=True, detail_context=context.get('detail_context', {}),
                request_constraints={'locked_item_ids': trip.get('locked_item_ids', [])},
                traveler_profile=trip.get('traveler_profile') or {},
                on_checkpoint=self.on_checkpoint, budget_seconds=max(0, deadline - time.monotonic()))
        else:
            plan, usage = self._initial(trip, request, context, validate, deadline, caller)
        review = usage.get('revision_review')
        if (not isinstance(review, dict) or review.get('verdict') != 'pass'
                or review.get('candidate_sha256') != plan_sha256(plan)
                or review.get('previous_sha256') != plan_sha256(trip.get('plan'))):
            raise StateError('REVIEW_MISMATCH', 'Missing matching real critic review', 409)
        return dict(plan=plan, review=review, assessment_ids=[], valid_until=self.clock() + self.ttl,
                    plan_touches=plan_touches(previous, plan, usage))

    def _initial(self, trip, request, context, validate, deadline, caller):
        usage = {'model_calls': 0, 'input_tokens': 0, 'output_tokens': 0,
                 'usage_complete': True, 'writer_calls': 0, 'critic_calls': 0}
        payload = dict(latest_request=request['instruction'], materials=context['materials'],
            source_evidence=context['evidence'], catalog=context['candidates'], previous_plan={},
            locked_item_ids=trip.get('locked_item_ids', []), detail_context=context.get('detail_context', {}),
            traveler_profile=trip.get('traveler_profile') or {})
        direction = context.get('direction')
        if not direction:
            direction, met = caller(core_prompts.direction_messages(payload), max_tokens=5000,
                timeout=self._remaining(deadline, 90), reasoning_effort='none')
            add_usage(usage, met)
        payload['direction'] = direction_view(direction, context['materials'])
        if payload['direction']['days'] >= SHARD_THRESHOLD:
            raw = compose_initial(caller=caller, payload=payload, usage=usage,
                                  on_checkpoint=self.on_checkpoint, autonomous=False)
        else:
            raw, met = caller(core_prompts.plan_messages(payload), max_tokens=16000,
                timeout=self._remaining(deadline, 120), reasoning_effort='none',
                model_role='writer', response_schema=WRITER_PLAN_SCHEMA)
            add_usage(usage, met)
        # Existing validators enforce complete day count, references, locks and
        # provenance before the semantic critic sees the exact resulting plan.
        validate_plan(raw, context['candidates'], {}, trip.get('locked_item_ids', []),
                                   payload['direction']['days'], request['instruction'])
        plan = validate(raw)
        diff = writer_critic.mechanical_diff({}, plan)
        review_payload = {**payload, 'phase': 'critic', 'feedback': request['instruction'],
                          'candidate': plan, 'mechanical_diff': diff}
        row = {}
        raw_review = writer_critic._invoke(caller, writer_critic.CRITIC, review_payload,
            usage, row, 'critic', lambda _: None, deadline=deadline)
        verdict, objections, checks = writer_critic._critic_verdict(raw_review)
        if verdict != 'pass':
            raise writer_critic.RevisionRejected(objections, usage)
        usage['revision_review'] = dict(verdict=verdict, checks=checks,
            previous_sha256=plan_sha256(trip.get('plan')), candidate_sha256=plan_sha256(plan),
            diff_sha256=plan_sha256(diff), limits='Model semantic review, not independent fact proof')
        return plan, usage

    @staticmethod
    def _remaining(deadline, cap):
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            raise PlanError('Autopilot execution deadline exhausted')
        return min(cap, remaining / 1.25)
