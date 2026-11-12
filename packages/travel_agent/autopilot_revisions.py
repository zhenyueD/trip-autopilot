"""Trusted reviewed proposals, atomic publication and conflict-aware selective undo.

The builder is server wiring, never a model tool argument. A missing production
adapter fails closed. No remote calls happen while a SQLite transaction is held.
"""
import copy
import json
from uuid import uuid4

from .autopilot_revision_fields import canonical, changes, inverse, ownership_version
from .autopilot_revision_guards import (assert_dependencies, assert_review, dependency_snapshot,
                                       expiry, iso_time, validate_candidate)
from .revision_plan import plan_sha256
from .store import StateError
from .uncertainty_store import UncertaintyStore


class AutopilotRevisions:
    def __init__(self, autopilot_store, *, candidate_builder=None, clock=None):
        self.state = autopilot_store
        self.trips = autopilot_store.trip_store
        self.clock = clock or autopilot_store.clock
        self.builder = candidate_builder
        self.records = UncertaintyStore(connection_factory=self.trips.connection,
                                        clock=lambda: iso_time(self.clock))
        with self.state.transaction() as db:
            for table in ('autopilot_proposals', 'autopilot_revisions'):
                db.execute(f'CREATE TABLE IF NOT EXISTS {table} '
                           '(id TEXT PRIMARY KEY, trip_id TEXT NOT NULL, data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS autopilot_plan_ownership '
                       '(trip_id TEXT PRIMARY KEY, version INTEGER NOT NULL, data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS autopilot_revision_builds '
                       '(operation_id TEXT PRIMARY KEY, started_at REAL NOT NULL)')

    def _row(self, db, table, record_id, trip_id):
        row = db.execute(f'SELECT data FROM {table} WHERE id=? AND trip_id=?',
                         (record_id, trip_id)).fetchone()
        if not row:
            raise StateError('NOT_FOUND', '记录不存在或不属于当前旅行', 404)
        return json.loads(row[0])

    def _insert(self, db, table, value):
        db.execute(f'INSERT INTO {table} VALUES (?,?,?)',
                   (value['id'], value['trip_id'], canonical(value)))

    def _context(self, db, trip_id, token, run_id, lease, operation_id, name, arguments):
        run = self.state._lease(db, run_id, lease)
        trip = self.trips._load(db, trip_id)
        if token is not None:
            self.trips._authorize(trip, token)
        if run['trip_id'] != trip_id:
            raise StateError('NOT_FOUND', '运行不属于当前旅行', 404)
        operation = self.state._load(db, 'autopilot_operations', operation_id)
        if (operation['run_id'] != run_id or operation['name'] != name
                or canonical(operation['arguments']) != canonical(arguments)):
            raise StateError('OPERATION_CONFLICT', '操作与请求不一致', 409)
        goal = self.state._load(db, 'autopilot_goals', run['goal_id'])
        if name not in goal['authorization']['tools']:
            raise StateError('TOOL_NOT_AUTHORIZED', '当前授权不允许此操作', 403)
        if operation['status'] == 'succeeded':
            return trip, goal, operation['result']
        self.state.assert_dispatch(run_id, lease, db=db)
        if operation['status'] != 'in_flight' or operation['lease_generation'] != lease['generation']:
            raise StateError('OPERATION_UNKNOWN', '操作状态不确定，不能重复派发', 409)
        return trip, goal, None

    def _ready(self, trip, revision):
        self.trips._check_revision(trip, revision)
        initial = trip.get('plan') is None and trip['status'] in {'running', 'ready', 'interrupted', 'error', 'needs_attention'}
        if not initial and trip['status'] != 'ready':
            raise StateError('BUSY', '当前完整方案尚未就绪', 409)

    def propose(self, trip_id, token, expected_revision, request, *, run_id, lease, operation_id):
        arguments = dict(expected_revision=expected_revision, request=request)
        context = (trip_id, token, run_id, lease, operation_id, 'propose_revision', arguments)
        with self.state.transaction() as db:
            trip, goal, replay = self._context(db, *context)
            if replay is not None:
                return replay
            self._ready(trip, expected_revision)
            if self.builder is None:
                raise StateError('BUILDER_UNAVAILABLE', '尚未配置可信 writer/critic 提案适配器', 503)
            if db.execute('SELECT 1 FROM autopilot_revision_builds WHERE operation_id=?',
                          (operation_id,)).fetchone():
                raise StateError('OPERATION_UNKNOWN', '候选生成已派发，不能猜测重试', 409)
            db.execute('INSERT INTO autopilot_revision_builds VALUES (?,?)', (operation_id, self.clock()))
            dependencies = dependency_snapshot(trip, goal, db)
            baseline_write_version = ownership_version(db, trip_id)
        # Builder must route every physical model request through the run budget.
        built = self.builder(copy.deepcopy(trip), copy.deepcopy(request))
        candidate = validate_candidate(built['plan'], trip)
        assert_review(candidate, trip['plan'], built['review'])
        proposal = self._proposal(trip, built, candidate, dependencies, expected_revision)
        proposal['baseline_write_version'] = baseline_write_version
        with self.state.transaction() as db:
            current, goal, replay = self._context(db, *context)
            if replay is not None:
                return replay
            self._ready(current, expected_revision)
            self._baseline(db, current, proposal)
            assert_dependencies(proposal, current, goal, self.records, db, self.clock())
            self._insert(db, 'autopilot_proposals', proposal)
            result = dict(proposal_id=proposal['id'], trip_id=trip_id, status='proposed',
                          expected_revision=expected_revision, valid_until=proposal['valid_until'])
            self.state.finish_operation(run_id, lease, operation_id, result, db=db)
        return self._after_commit(result)

    def _baseline(self, db, trip, proposal):
        if ownership_version(db, trip['id']) != proposal['baseline_write_version']:
            raise StateError('VERSION_CONFLICT', '计划字段写入归属已变化', 409)

    def _proposal(self, trip, built, candidate, dependencies, expected_revision):
        review = copy.deepcopy(built['review'])
        return dict(id=uuid4().hex, trip_id=trip['id'], expected_revision=expected_revision,
                    plan=candidate, previous=trip['plan'], review=review,
                    review_sha256=plan_sha256(review), dependencies=dependencies,
                    assessment_ids=copy.deepcopy(built.get('assessment_ids', [])),
                    plan_touches=copy.deepcopy(built.get('plan_touches')),
                    valid_until=expiry(built['valid_until'], self.clock()), created_at=self.clock())

    def get_proposal(self, proposal_id, *, trip_id, token):
        with self.state.transaction() as db:
            trip = self.state._trip(db, trip_id, token)
            proposal = self._row(db, 'autopilot_proposals', proposal_id, trip_id)
            goal = self.state._load(db, 'autopilot_goals', proposal['dependencies']['goal_id'])
            assert_dependencies(proposal, trip, goal, self.records, db, self.clock())
            return proposal

    def trusted_get_proposal(self, run_id, lease, proposal_id):
        with self.state.transaction() as db:
            run = self.state._lease(db, run_id, lease)
            trip = self.trips._load(db, run['trip_id'])
            proposal = self._row(db, 'autopilot_proposals', proposal_id, run['trip_id'])
            goal = self.state._load(db, 'autopilot_goals', run['goal_id'])
            assert_dependencies(proposal, trip, goal, self.records, db, self.clock())
            return proposal

    def apply(self, proposal_id, expected_revision, *, trip_id, token, run_id, lease, operation_id):
        arguments = dict(proposal_id=proposal_id, expected_revision=expected_revision)
        with self.state.transaction() as db:
            trip, goal, replay = self._context(db, trip_id, token, run_id, lease,
                                               operation_id, 'apply_revision', arguments)
            if replay is not None:
                return replay
            self._ready(trip, expected_revision)
            proposal = self._row(db, 'autopilot_proposals', proposal_id, trip_id)
            self._baseline(db, trip, proposal)
            if proposal['expected_revision'] != expected_revision:
                raise StateError('VERSION_CONFLICT', '提案基线已变化', 409)
            assert_dependencies(proposal, trip, goal, self.records, db, self.clock())
            candidate = validate_candidate(proposal['plan'], trip)
            assert_review(candidate, trip['plan'], proposal['review'])
            result = self._publish(db, trip, candidate, proposal, kind='apply')
            self.state.finish_operation(run_id, lease, operation_id, result, db=db)
        return self._after_commit(result)

    def _explicit_user_undo(self, db, run_id, arguments):
        # This is persisted by the authenticated command service, never supplied
        # as a model tool argument. Exact command binding prevents authority reuse.
        run = self.state._load(db, 'autopilot_runs', run_id)
        event = run.get('event')
        if not isinstance(event, dict) or event.get('type') != 'user_command':
            return False
        expected = dict(name='undo_revision', arguments=arguments)
        return canonical(event.get('server_command')) == canonical(expected)

    def undo(self, revision_id, expected_revision, *, trip_id, token, run_id, lease, operation_id):
        arguments = dict(revision_id=revision_id, expected_revision=expected_revision)
        with self.state.transaction() as db:
            trip, goal, replay = self._context(db, trip_id, token, run_id, lease,
                                               operation_id, 'undo_revision', arguments)
            if replay is not None:
                return replay
            self._ready(trip, expected_revision)
            revision = self._row(db, 'autopilot_revisions', revision_id, trip_id)
            proposal = self._row(db, 'autopilot_proposals', revision['proposal_id'], trip_id)
            explicit = self._explicit_user_undo(db, run_id, arguments)
            assert_dependencies(proposal, trip, goal, self.records, db, self.clock(),
                                undo=True, explicit_user_undo=explicit)
            candidate = inverse(db, trip, revision)
            if candidate is None:
                raise StateError('UNDO_CONFLICT', '初始生成没有可恢复的完整旧方案，请创建调整提案', 409)
            validated = validate_candidate(candidate, trip)
            if plan_sha256(validated) != plan_sha256(candidate):
                raise StateError('UNDO_CONFLICT', '撤销需要额外修改，请生成新提案', 409)
            result = self._publish(db, trip, candidate, proposal, kind='undo', undo_of=revision_id)
            self.state.finish_operation(run_id, lease, operation_id, result, db=db)
        return self._after_commit(result)

    def _publish(self, db, trip, candidate, proposal, *, kind, undo_of=None):
        from .store_timeline import timeline_commit_fields
        from .trip_graph import build_trip_graph
        revision_id = uuid4().hex
        delta = changes(trip['plan'], candidate)
        result = dict(revision_id=revision_id, proposal_id=proposal['id'], trip_id=trip['id'],
                      revision=trip['revision'] + 1, kind=kind, changes=delta,
                      committed_at=self.clock(), undo_of=undo_of)
        record = dict(id=revision_id, before=copy.deepcopy(trip['plan']), after=copy.deepcopy(candidate), **result)
        timeline = timeline_commit_fields(trip, candidate, result['revision'], kind=kind)
        trip['history'] = (trip.get('history', []) + [dict(plan=copy.deepcopy(trip['plan']),
            plan_revision=trip.get('plan_revision'), feedback=trip['feedback'], locks=trip['locked_item_ids'])])[-5:]
        trip.update(plan=copy.deepcopy(candidate), revision=result['revision'], status='ready', error=None,
                    autopilot_revision_receipt=result, **timeline)
        trip.pop('revision_receipt', None)
        direction = dict(trip.get('core_direction') or {},
                         understanding=candidate.get('understanding', []), questions=candidate.get('questions', []))
        graph = build_trip_graph(trip.get('material_sources') or [], direction, candidate)
        graph.update(plan_revision=trip['revision'], plan_sha256=plan_sha256(candidate),
                     dependency_sha256=plan_sha256(proposal['dependencies']))
        trip['trip_graph'] = graph
        touches = [row['path'] for row in delta]
        if kind == 'apply':
            touches = proposal.get('plan_touches') if proposal.get('plan_touches') is not None else [[]]
        self.trips._save(db, trip, plan_write=True, plan_touches=touches,
                         plan_owner=revision_id)
        record['write_version'] = ownership_version(db, trip['id'])
        self._insert(db, 'autopilot_revisions', record)
        return result

    def _after_commit(self, result):
        """Fault-injection seam: a crash here must replay the durable result."""
        return result
