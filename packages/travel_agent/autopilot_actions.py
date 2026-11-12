"""Server-wired actions. External/model material never gains write authority."""
import copy
import hashlib

from .revision_plan import plan_sha256, validate_revision_plan
from .public_search import TOPIC_QUERY, public_source_language
from .store import StateError
from .validation import PlanError
from .autopilot_verifier import evaluate_criteria
from .autopilot_advice import evaluate_advice


def _unavailable(name):
    raise StateError('CAPABILITY_UNAVAILABLE', name + ' is not configured', 503)


def _text(value, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise StateError('INVALID_ARGUMENT', 'Expected bounded nonempty text', 400)
    return value


def _strings(values):
    if not isinstance(values, (list, tuple)) or len(values) > 20:
        raise StateError('INVALID_ARGUMENT', 'Expected bounded string list', 400)
    return [_text(value, 500) for value in values]


class AutopilotActions:
    """Dependencies are trusted server callbacks, never model arguments.

    researcher(minimal_query) -> full original source records; source_sink(record)
    -> durable source ID readable by the workspace. goal_evaluator(payload) ->
    per-criterion verdicts with quote, support and applicability assessments.
    evidence_resolver(id) resolves the evaluator's frozen authorized originals.
    assessment_sink(record) -> durable assessment ID. All callbacks run in the
    caller's current_budget context; they must not replace/reset that budget.
    """
    def __init__(self, *, candidate_builder=None, researcher=None, source_sink=None,
                 assessment_sink=None, goal_evaluator=None, evidence_resolver=None,
                 delegate_worker=None):
        self.candidate_builder = candidate_builder
        self.researcher, self.source_sink = researcher, source_sink
        self.assessment_sink, self.goal_evaluator = assessment_sink, goal_evaluator
        self.evidence_resolver, self.delegate_worker = evidence_resolver, delegate_worker

    def available_tools(self):
        names = ['check_constraints']
        if self.researcher is not None and self.source_sink is not None:
            names.append('research_fact')
        if self.delegate_worker is not None:
            names.append('delegate')
        return names

    def research_fact(self, query, *, entities=(), dates=(), conditions=(), source_ids=(), topic=None,
                      source_languages=None, timezone=None):
        if self.researcher is None or self.source_sink is None:
            _unavailable('research_fact')
        payload = dict(query=_text(query), entities=_strings(entities),
                       dates=_strings(dates), conditions=_strings(conditions))
        if timezone is not None:
            payload['timezone'] = _text(timezone, 100)
        if source_ids:
            payload['source_ids'] = _strings(source_ids)
        if source_languages is not None:
            if (not isinstance(source_languages, list) or len(source_languages) > 3
                    or any(not isinstance(value, str) or len(value) > 32
                           or public_source_language(value) is None for value in source_languages)):
                raise StateError('INVALID_ARGUMENT', 'Expected at most three source language tags', 400)
            payload['source_languages'] = list(dict.fromkeys(map(public_source_language, source_languages)))
        if topic is not None:
            if not isinstance(topic, str) or topic not in TOPIC_QUERY:
                raise StateError('INVALID_ARGUMENT', 'Unknown public research topic', 400)
            payload['topic'] = topic
        records = self.researcher(copy.deepcopy(payload))
        if not isinstance(records, list) or len(records) > 20:
            raise StateError('INVALID_SOURCE_RESULT', 'Expected bounded source records', 502)
        ids, failures = [], []
        for source in records:
            if not isinstance(source, dict):
                raise StateError('INVALID_SOURCE_RESULT', 'Invalid source receipt', 502)
            record = copy.deepcopy(source)
            body = record.get('text')
            if record.get('status') != 'read' or not isinstance(body, str) or not body.strip():
                failures.append(record)
            else:
                record['content_sha256'] = hashlib.sha256(body.encode()).hexdigest()
            # Failed and truncated originals also persist; never upgrade a snippet
            # into complete evidence or silently discard negative evidence.
            record['research_request'] = copy.deepcopy(payload)
            record.setdefault('applicability', {'status': 'not_evaluated'})
            id_ = self.source_sink(record)
            ids.append(_text(id_, 500))
        return dict(status='read' if ids and len(failures) < len(ids) else 'unavailable', source_ids=ids,
                    failed_sources=len(failures), fact_confirmed=None,
                    applicability='not_evaluated')

    def check_constraints(self, trip, goal, *, plan=None, advice=None):
        if advice is not None:
            return self._check_advice(trip, goal, advice)
        candidate = copy.deepcopy(trip.get('plan') if plan is None else plan)
        previous = trip.get('plan') or {}
        result = {'plan_sha256': plan_sha256(candidate), 'goal_id': goal['id'],
                  'goal_version': goal['version'],
                  'criteria_sha256': plan_sha256(goal['success_criteria']),
                  'constraints_sha256': plan_sha256(goal.get('constraints', {})),
                  'goal_satisfied': None, 'semantic': {'status': 'not_evaluated'}}
        try:
            validate_revision_plan(candidate, previous, previous.get('supplies', []),
                trip.get('locked_item_ids', []), sources=(trip.get('source_evidence') or []) +
                (trip.get('material_sources') or []))
            result['mechanical'] = {'status': 'pass'}
        except (PlanError, TypeError, ValueError, KeyError) as error:
            result['mechanical'] = {'status': 'fail', 'reason': str(error)[:1200]}
        if self.goal_evaluator is not None and self.evidence_resolver is not None:
            result['semantic'], satisfied = self._evaluate(candidate, goal)
            result['goal_satisfied'] = satisfied if result['mechanical']['status'] == 'pass' else False
        if self.assessment_sink is not None:
            result['assessment_id'] = _text(self.assessment_sink(copy.deepcopy(result)), 500)
        return result

    def _check_advice(self, trip, goal, advice):
        result = dict(scope='advice', advice=copy.deepcopy(advice),
            **{key: advice[key] for key in ('advice_id', 'text_sha256', 'candidate_sha256')},
            goal_id=goal['id'], goal_version=goal['version'], goal_satisfied=None,
            trip_revision=trip['revision'], current_plan_sha256=plan_sha256(trip.get('plan')),
            verdict='unknown', semantic={'status': 'not_evaluated'})
        if self.goal_evaluator is not None and self.evidence_resolver is not None:
            raw = self.goal_evaluator(dict(advice=copy.deepcopy(advice), goal=copy.deepcopy(goal)))
            result['semantic'], result['verdict'] = evaluate_advice(raw, advice, self.evidence_resolver)
            feedback = result['semantic'].pop('feedback', None)
            if feedback is not None:
                result['feedback'] = feedback
        if self.assessment_sink is not None:
            result['assessment_id'] = _text(self.assessment_sink(copy.deepcopy(result)), 500)
        return result

    def _evaluate(self, plan, goal):
        criteria = goal['success_criteria']
        payload = dict(plan=copy.deepcopy(plan), goal={key: copy.deepcopy(goal.get(key))
            for key in ('id', 'version', 'objective', 'success_criteria', 'constraints')})
        raw = self.goal_evaluator(payload)
        return evaluate_criteria(raw, criteria, self.evidence_resolver)

    def delegate(self, task, *, purpose, evidence_ids=()):
        if self.delegate_worker is None:
            _unavailable('delegate')
        if purpose not in ('evidence', 'counterexamples', 'materials'):
            raise StateError('INVALID_ARGUMENT', 'Delegate only evidence/counterexamples/materials', 400)
        payload = dict(task=_text(task), purpose=purpose, evidence_ids=_strings(evidence_ids))
        material = self.delegate_worker(copy.deepcopy(payload))
        # The result is inert material even if it contains instructions or a
        # claimed commit. No store, tool gateway, or publication callback here.
        plan_sha256(material)
        return dict(status='returned', purpose=purpose, material=material, committed=False)
