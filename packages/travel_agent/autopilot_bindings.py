"""Bind existing travel algorithms to the durable agent; no alternate planner."""
import copy
import json
import unicodedata
from uuid import uuid4

from .autopilot_action_candidate import TrustedCandidateBuilder, default_context
from .autopilot_action_research import AdmittedSourceResearch, EvidenceDelegate
from .autopilot_actions import AutopilotActions
from .autopilot_advice import ADVICE_VERIFIER_PROMPT, advice_current, make_advice
from .autopilot_contract import canonical, fingerprint
from .autopilot_gateway import ToolGateway, recorded_handler
from .autopilot_final import OutcomeValidator
from .autopilot_methods import load_method, method_catalog
from .autopilot_revision_guards import assert_dependencies, dependency_snapshot
from .autopilot_revisions import AutopilotRevisions
from .autopilot_workspace import AutopilotWorkspace
from .autopilot_workspace_records import material_state, source_freshness
from .autopilot_verifier import VERIFIER_PROMPT, evidence_snapshot
from .intake import check_source_budget
from .model import call_planner_json
from .prompts import messages
from .store import StateError
from .uncertainty_contract import UncertaintyError


class ToolBindings:
    def __init__(self, store, *, candidate_builder=None, planner_caller=None, source_reader=None):
        self.store = store
        self.workspace = AutopilotWorkspace(store)
        self.planner_caller = planner_caller or call_planner_json
        self.source_reader = source_reader
        builder = candidate_builder or TrustedCandidateBuilder(caller=self.planner_caller,
            context_builder=self._candidate_context, clock=store.clock)
        self.revisions = AutopilotRevisions(store, candidate_builder=builder)
        handlers = {name: self._workspace_handler(name) for name in sorted(self.workspace.tools)}
        handlers.update({name: self._revision_handler(name) for name in
                         ('propose_revision', 'apply_revision', 'undo_revision')})
        handlers.update({name: self._action_handler(name)
                         for name in ('research_fact', 'check_constraints', 'delegate')})
        handlers['load_method'] = recorded_handler(store, 'load_method', self._load_method)
        self.gateway = ToolGateway(store, handlers)
        self.outcomes = OutcomeValidator(store, resolver=self.resolve_outcome)

    def _workspace_handler(self, name):
        return lambda run_id, lease, call_id, args: self.workspace.execute(run_id, lease, call_id, name, args)

    def _load_method(self, context, args):
        available = [row['function']['name'] for row in
                     self.gateway.catalog(context['run_id'], context['lease'])]
        return load_method(args['method_id'], args['version'], available)

    def _action_handler(self, name):
        recorded = recorded_handler(self.store, name, self._action(name))
        def handle(run_id, lease, call_id, args):
            result = recorded(run_id, lease, call_id, args)
            trip = self._trip(dict(run_id=run_id, lease=lease))
            if ('material_state' in result and
                    result['material_state'] != material_state(trip, self.store.clock())):
                raise StateError('STALE_OPERATION_RESULT', '来源适用状态已变化，旧结果仅供历史核对', 409)
            if result.get('scope') == 'advice':
                with self.store.transaction() as db:
                    run = self.store._lease(db, run_id, lease)
                    current = self.store.trip_store._load(db, run['trip_id'])
                    if not self.resolve_outcome(db, run, current, result.get('advice_id')):
                        raise StateError('STALE_OPERATION_RESULT', '建议或依赖已变化，须重新核验', 409)
            return result
        return handle

    def _candidate_context(self, trip, request):
        context = default_context(trip, request, clock=self.store.clock)
        with self.store.transaction() as db:
            context['detail_context'] = self.workspace.context_user_state(db, trip)
            ledger = self.workspace.reusable_detail_ledger(db, trip)
            if ledger is not None:
                context['detail_context']['detail_ledger'] = ledger
        return context

    def snapshot(self, run_id, lease):
        snapshot = self.workspace.trusted_context_snapshot(run_id, lease)
        run = self.store.internal_get_run(run_id)
        goal = self.store.internal_get_goal(run['goal_id'])
        snapshot['trip']['id'] = run['trip_id']
        snapshot['trip']['material_sources'] = snapshot['materials']
        snapshot['trip']['answered_inputs'] = [r for r in snapshot['inputs'] if r.get('status') == 'answered']
        with self.store.transaction() as db:
            trip = self.store.trip_store._load(db, run['trip_id'])
            user_state = self.workspace.context_user_state(db, trip)
            goal = self.workspace.context_goal(db, trip, goal)
            snapshot['material_state'] = fingerprint({'materials': material_state(trip, self.store.clock()),
                'evidence': trip.get('source_evidence', []), 'goal_version': goal['version'],
                'user_state': user_state})
            snapshot['trip']['corrections'] = user_state['user_corrections']
            snapshot['trip']['answered_inputs'] = user_state['answered_inputs']
        snapshot.update(run=run, goal=goal)
        snapshot['trip']['method_catalog'] = method_catalog(
            row['function']['name'] for row in self.gateway.catalog(run_id, lease))
        return snapshot

    def _revision_handler(self, name):
        def handle(run_id, lease, call_id, args):
            reserved = self.store.reserve_operation(run_id, lease, call_id, name, args)
            operation = reserved['operation']
            if reserved['disposition'] == 'replay':
                return operation['result']
            if reserved['disposition'] != 'execute':
                raise StateError('OPERATION_UNKNOWN', '操作结果待核对', 409)
            run = self.store.internal_get_run(run_id)
            context = dict(trip_id=run['trip_id'], token=None, run_id=run_id, lease=lease,
                           operation_id=operation['id'])
            try:
                if name == 'propose_revision':
                    return self.revisions.propose(**args, **context)
                if name == 'apply_revision':
                    return self.revisions.apply(**args, **context)
                return self.revisions.undo(**args, **context)
            except (StateError, UncertaintyError) as error:
                current = self.store.internal_get_operation(run_id, lease, call_id)
                if current['status'] == 'succeeded':
                    return current['result']
                result = {'status': 'rejected', 'error_code': error.code}
                self.store.finish_operation(run_id, lease, operation['id'], result)
                return result
        return handle

    def _trip(self, context):
        with self.store.transaction() as db:
            run = self.store._lease(db, context['run_id'], context['lease'])
            return self.store.trip_store._load(db, run['trip_id'])

    def _source(self, context, source_id):
        trip = self._trip(context)
        if source_id == 'current-plan:' + fingerprint(trip.get('plan')):
            if trip.get('workspace_stale_plan_hash') == fingerprint(trip.get('plan')):
                return None
            return {'kind': 'current_plan_state', 'plan': trip.get('plan'), 'fact_verified': False}
        source = next((s for s in trip.get('material_sources', []) if s.get('id') == source_id), None)
        return dict(copy.deepcopy(source), **source_freshness(source, self.store.clock())) if source and self.workspace._visible(source) else None

    def _save_source(self, context, record):
        with self.store.transaction() as db:
            run = self.store.assert_dispatch(context['run_id'], context['lease'], db=db)
            trip = self.store.trip_store._load(db, run['trip_id'])
            original = record.get('text') if record.get('status') == 'read' else ''
            source = dict(id='research-' + uuid4().hex, kind='link', label=record.get('url', '公开来源'),
                content=original or '', status=record.get('status', 'unknown'), url=record.get('url'),
                observed_at=self.store.clock(), workspace_version=1,
                reading_receipt=copy.deepcopy(record), applicability=record.get('applicability'))
            trip.setdefault('material_sources', []).append(source)
            check_source_budget(trip['material_sources'])
            self.store.trip_store._save(db, trip)
            return source['id']

    def _verification_payload(self, context, payload):
        trip = self._trip(context)
        if trip.get('workspace_stale_plan_hash') == fingerprint(trip.get('plan')):
            raise StateError('SOURCE_RECHECK_REQUIRED', '原方案依赖的材料已撤回，尚不能发送派生内容复核', 409)
        goal = self.store.internal_get_goal(context['goal_id'])
        if any(goal[key] != payload['goal'][key] for key in ('id', 'version')):
            raise StateError('GOAL_VERSION_CONFLICT', '复核目标版本已变化', 409)
        with self.store.transaction() as db:
            user_state = self.workspace.context_user_state(db, trip)
            goal = self.workspace.context_goal(db, trip, goal)
            run = self.store._lease(db, context['run_id'], context['lease'])
            work = [self.workspace._public(row) for row in self.workspace._rows(db, trip['id'], 'work')
                    if row.get('status') not in {'completed', 'cancelled'} and self.workspace._valid(trip, row)]
            receipts = []
            for saved in db.execute('SELECT data FROM autopilot_operations WHERE run_id=?', (context['run_id'],)):
                row = json.loads(saved[0])
                result = row.get('result')
                if (row['status'] == 'succeeded' and row['name'] in {
                        'apply_revision', 'undo_revision', 'schedule_check', 'request_input'}
                        and isinstance(result, dict) and not result.get('error_code')
                        and result.get('material_state', material_state(trip, self.store.clock())) == material_state(trip, self.store.clock())):
                    targets = [result[key] for key in ('job_id', 'pending_input_id') if result.get(key)]
                    if all(self.resolve_outcome(db, run, trip, target) for target in targets):
                        receipts.append(dict({key: row.get(key) for key in ('id', 'name', 'finished_at', 'result')},
                                             target_refs=targets))
        sources = [dict(copy.deepcopy(source), **source_freshness(source, self.store.clock()))
                   for source in trip.get('material_sources', []) if self.workspace._visible(source)]
        advice = payload.get('advice')
        if advice is not None:
            available = {source['id'] for source in sources}
            if set(advice['source_ids']) - available:
                raise StateError('ADVICE_SOURCE_UNAVAILABLE', '建议来源不可见或已撤回', 409)
            sources = [source for source in sources if source['id'] in advice['source_ids']]
        evidence = evidence_snapshot(payload.get('plan'), trip, goal, user_state, sources, receipts)
        candidate = dict(plan_evidence_id=evidence[0]['id'], plan_sha256=evidence[0]['plan_sha256'],
                         trip_revision=trip['revision'])
        if advice is not None:
            evidence[0] = dict(id=advice['advice_id'], proof_kind='advice', content=advice['text'],
                               text_sha256=advice['text_sha256'], candidate_sha256=advice['candidate_sha256'])
            candidate = dict(kind='advice', advice=copy.deepcopy(advice),
                advice_evidence_id=advice['advice_id'], text_sha256=advice['text_sha256'],
                candidate_sha256=advice['candidate_sha256'], trip_revision=trip['revision'])
        return dict(goal={key: goal[key] for key in ('id', 'version', 'objective', 'success_criteria', 'constraints')},
            candidate=candidate,
            user_state=user_state, open_work=work, evidence=evidence)

    def _evaluator(self, context, frozen_evidence=None):
        def evaluate(payload):
            bounded = self._verification_payload(context, payload)
            if len(canonical(bounded).encode()) > 64000:
                raise StateError('CONTEXT_LIMIT', '目标复核上下文过大，尚未完成语义验收', 409)
            if frozen_evidence is not None:
                frozen_evidence.update({row['id']: copy.deepcopy(row) for row in bounded['evidence']})
                if len(frozen_evidence) != len(bounded['evidence']):
                    raise StateError('AMBIGUOUS_EVIDENCE', '证据标识重复，无法确定原文', 409)
            prompt = VERIFIER_PROMPT + (ADVICE_VERIFIER_PROMPT if payload.get('advice') is not None else '')
            raw, _ = self.planner_caller(messages(prompt, bounded), max_tokens=3000, timeout=60,
                                         reasoning_effort='none')
            if payload.get('advice') is not None:
                # Private operation metadata, persisted before any normalization.
                # Never return provider extras or reasoning through tool history.
                with self.store.transaction() as db:
                    self.store._lease(db, context['run_id'], context['lease'])
                    operation = self.store._load(db, 'autopilot_operations', context['operation_id'])
                    if (operation['run_id'] != context['run_id'] or operation['status'] != 'in_flight'
                            or operation['lease_generation'] != context['lease']['generation']):
                        raise StateError('OPERATION_CONFLICT', '复核操作已变化', 409)
                    operation['review'] = dict(raw_response=copy.deepcopy(raw), raw_sha256=fingerprint(raw),
                        **{key: payload['advice'][key] for key in ('advice_id', 'text_sha256', 'candidate_sha256')},
                        assessment_id=context['assessment_id'], operation_id=operation['id'], call_id=operation['call_id'])
                    self.store._save(db, 'autopilot_operations', operation)
            return raw
        return evaluate

    def _action(self, name):
        def act(context, args):
            trip = self._trip(context)
            source_state = material_state(trip, self.store.clock())
            goal = self.store.internal_get_goal(context['goal_id'])
            with self.store.transaction() as db:
                dependency_hash = fingerprint(dependency_snapshot(trip, goal, db))
                user_state_hash = fingerprint(self.workspace.context_user_state(db, trip))
            resolver = lambda source_id: self._source(context, source_id)
            frozen_evidence = {}
            assessment_id = 'assessment-' + uuid4().hex
            actions = AutopilotActions(researcher=AdmittedSourceResearch(resolver, reader=self.source_reader,
                audit_sink=lambda request: self.store.record_research_request(
                    context['run_id'], context['lease'], context['operation_id'], request)),
                source_sink=lambda record: self._save_source(context, record),
                assessment_sink=lambda record: assessment_id,
                goal_evaluator=self._evaluator(dict(context, assessment_id=assessment_id), frozen_evidence), evidence_resolver=frozen_evidence.get,
                delegate_worker=EvidenceDelegate(resolver, caller=self.planner_caller))
            if name == 'research_fact':
                entity = args['entity']
                if not args.get('source_ids'):
                    entity = self._public_entity(goal, entity)
                result = actions.research_fact(args['question'], entities=[entity],
                    dates=[args['date']] if args.get('date') else [], conditions=args.get('conditions', []),
                    timezone=args.get('timezone'),
                    source_ids=args.get('source_ids', []), topic=args.get('topic'),
                    source_languages=args.get('source_languages'))
            elif name == 'delegate':
                result = actions.delegate(args['instruction'],
                    purpose={'counterexample': 'counterexamples'}.get(args['task_type'], args['task_type']),
                    evidence_ids=args.get('source_ids', []))
            else:
                if args.get('advice') is not None and (args.get('proposal_id') or args.get('source_ids')):
                    raise StateError('INVALID_ADVICE_TARGET', '建议与方案目标不可混用', 400)
                advice = make_advice(args['advice'], context['operation_id']) if 'advice' in args else None
                proposal = self.revisions.trusted_get_proposal(context['run_id'], context['lease'],
                    args['proposal_id']) if args.get('proposal_id') else None
                result = actions.check_constraints(trip, goal, plan=proposal['plan'] if proposal else None,
                                                   advice=advice)
                result['valid_until'] = self.store.clock() + 120
                result['dependency_sha256'] = dependency_hash
                result['user_state_sha256'] = user_state_hash
            result['material_state'] = (material_state(self._trip(context), self.store.clock())
                                        if name == 'research_fact' else source_state)
            return result
        return act

    @staticmethod
    def _public_entity(goal, requested):
        """Only authenticated user scope can supply outbound query text."""
        normalize = lambda value: unicodedata.normalize('NFC', value).strip().casefold()
        allowed = goal.get('constraints', {}).get('public_search_entities', [])
        if isinstance(requested, str) and isinstance(allowed, list):
            for entity in allowed:
                if isinstance(entity, str) and entity.strip() and normalize(entity) == normalize(requested):
                    return entity.strip()
        raise StateError('PUBLIC_RESEARCH_SCOPE_REQUIRED',
                         '公开搜索地点尚未授权；可使用已有材料，或由用户补充公开查询范围', 403)

    def resolve_outcome(self, db, run, trip, reference):
        """Current authority check at final delivery, beyond the model's cited ID."""
        if not isinstance(reference, str):
            return None
        row = db.execute('SELECT data FROM autopilot_proposals WHERE id=? AND trip_id=?',
                         (reference, trip['id'])).fetchone()
        if row:
            proposal = json.loads(row[0])
            goal = self.store._load(db, 'autopilot_goals', run['goal_id'])
            try:
                if proposal['expected_revision'] != trip['revision']:
                    return None
                assert_dependencies(proposal, trip, goal, self.revisions.records, db, self.store.clock())
            except StateError:
                return None
            return {'kind': 'proposal', 'value': proposal}
        row = db.execute('SELECT data FROM autopilot_revisions WHERE id=? AND trip_id=?',
                         (reference, trip['id'])).fetchone()
        if row:
            return {'kind': 'revision', 'value': json.loads(row[0])}
        row = db.execute('SELECT data FROM autopilot_workspace WHERE id=? AND trip_id=?',
                         (reference, trip['id'])).fetchone()
        if row:
            value = json.loads(row[0])
            if not self.workspace._valid(trip, value):
                return None
            if value['kind'] == 'input' and value['status'] == 'pending':
                return {'kind': 'input', 'value': value}
            if value['kind'] == 'job' and self.workspace._job_valid(db, value):
                if value['status'] in {'pending', 'claimed'}:
                    return {'kind': 'job', 'value': self.workspace._public(value)}
                target = (value.get('result') or {}).get('run_id')
                if value['status'] == 'succeeded' and target:
                    queued = self.store._load(db, 'autopilot_runs', target)
                    if queued['status'] in {'pending', 'running'}:
                        return {'kind': 'job', 'value': self.workspace._public(value)}
            return None
        records = self.outcomes._records(db, run, trip)
        record = records.get(reference)
        if record and (record['kind'] in {'assessment', 'advice', 'advice_assessment'}
                       or record['value'].get('scope') == 'advice'):
            goal = self.store._load(db, 'autopilot_goals', run['goal_id'])
            if record['value'].get('scope') == 'advice' and not advice_current(
                    record['value'], run, trip, self.store.clock()):
                return None
            if record['value'].get('dependency_sha256') != fingerprint(dependency_snapshot(trip, goal, db)):
                return None
            if record['value'].get('user_state_sha256') != fingerprint(self.workspace.context_user_state(db, trip)):
                return None
            for binding in record['value'].get('semantic', {}).get('evidence_bindings', {}).values():
                if any(not self.resolve_outcome(db, run, trip, target) for target in binding.get('target_refs', [])):
                    return None
            return record
        if record and record['kind'] == 'receipt':
            return record
        return None
