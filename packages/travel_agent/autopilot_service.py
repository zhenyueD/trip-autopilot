"""User-authorized application service. Explicit DB, bounded budgets, flags off."""
import hashlib
import json
import os
import re
import unicodedata
from uuid import uuid4

from .autopilot_bindings import ToolBindings
from .autopilot_contract import canonical, fail, fingerprint, limits, text
from .autopilot_model import call_tools
from .autopilot_intake import append_materials, check_revision, parse_materials
from .intake import material_receipts
from .autopilot_runner import AutopilotRunner
from .autopilot_service_transactions import ServiceStore
from .autopilot_service_views import ViewMethods
from .autopilot_worker import AutopilotWorker
from .model import ModelError, writer_settings
from .store import TripStore, public_trip

DEFAULT_LIMITS = dict(model_calls=16, input_tokens=128000, output_tokens=32000,
                      wall_seconds=600, tool_calls=32)


def configured_limits():
    return limits({key: int(os.environ.get('TRAVEL_AUTOPILOT_' + key.upper(), value))
                   for key, value in DEFAULT_LIMITS.items()})


class AutopilotService(ViewMethods):
    def __init__(self, trip_store, *, enabled=None, model_enabled=None, model_caller=None,
                 run_limits=None, bindings_factory=ToolBindings, store_factory=ServiceStore):
        if not isinstance(trip_store, TripStore):
            raise TypeError('Explicit TripStore instance required')
        self.enabled = os.getenv('TRAVEL_AUTOPILOT_ENABLED') == '1' if enabled is None else enabled
        self.model_enabled = (os.getenv('TRAVEL_AUTOPILOT_MODEL_ENABLED') == '1'
                              if model_enabled is None else model_enabled)
        self._uses_native = model_caller is None
        self.trips = trip_store
        self.run_limits = limits(run_limits) if run_limits is not None else configured_limits()
        self.store = store_factory(trip_store)
        self.bindings = bindings_factory(self.store)
        self.workspace = self.bindings.workspace
        self.runner = AutopilotRunner(self.store, self.bindings.gateway,
            snapshot=self.bindings.snapshot, model_caller=model_caller or self._native_call,
            outcome_validator=self.bindings.outcomes)
        self.worker = AutopilotWorker(self.store, self.workspace, self.runner)
        with self.store.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS autopilot_requests '
                       '(id TEXT PRIMARY KEY, trip_id TEXT NOT NULL, data TEXT NOT NULL)')

    def _native_call(self, messages, **kwargs):
        self._model_guard()
        return call_tools(messages, config=writer_settings(), **kwargs)

    def _model_guard(self):
        if not self.enabled:
            fail('AUTOPILOT_DISABLED', 404)
        if not self.model_enabled:
            fail('MODEL_EXECUTION_DISABLED', 503)
        if not self._native_configuration()['native_available']:
            fail('MODEL_CONFIGURATION_UNSUPPORTED', 503)

    def start(self):
        if self.enabled and self.model_enabled and self._native_configuration()['native_available']:
            self.worker.start()

    def capabilities(self):
        return dict(enabled=bool(self.enabled), model_enabled=bool(self.model_enabled),
                    tools=sorted(self.bindings.gateway.handlers), external_transactions=False,
                    run_limits=dict(self.run_limits), future_run_limits=dict(self.run_limits),
                    **self._native_configuration())

    def _native_configuration(self):
        if not self._uses_native:
            return dict(native_model='injected_caller', native_available=True,
                        native_configuration_reason=None)
        try:
            config = writer_settings()
        except ModelError:
            return dict(native_model=None, native_available=False,
                        native_configuration_reason='MODEL_CONFIGURATION_UNSUPPORTED')
        supported = (config.get('model') == 'deepseek-v4-pro' and not config.get('off')
                     and bool(config.get('key')) and bool(config.get('base')))
        return dict(native_model=config.get('model'), native_available=bool(supported),
                    native_configuration_reason=None if supported else 'MODEL_CONFIGURATION_UNSUPPORTED')

    def _key(self, scope, action, body):
        request_id = text(body.get('request_id'))
        if len(request_id) > 128:
            fail('INVALID_ARGUMENT', 400)
        return fingerprint([scope, action, request_id])

    def _replay(self, db, key, body):
        row = db.execute('SELECT data FROM autopilot_requests WHERE id=?', (key,)).fetchone()
        if not row:
            return None
        saved = json.loads(row[0])
        if saved['body_hash'] != fingerprint(body):
            fail('IDEMPOTENCY_CONFLICT')
        return saved['response']

    def _receipt(self, db, key, trip_id, body, response):
        db.execute('INSERT INTO autopilot_requests VALUES (?,?,?)',
            (key, trip_id, canonical(dict(body_hash=fingerprint(body), response=response))))
        return response

    def _goal(self, db, trip_id):
        rows = db.execute('SELECT data FROM autopilot_goals ORDER BY rowid DESC')
        for row in rows:
            goal = json.loads(row[0])
            if (goal['trip_id'] == trip_id and goal['status'] == 'active'
                    and not goal.get('command_only')):
                return goal
        fail('GOAL_NOT_ACTIVE')

    def _constraints(self, body):
        auto = body.get('auto_apply', False)
        if type(auto) is not bool:
            fail('INVALID_ARGUMENT', 400)
        result = {'auto_apply': auto,
                  'public_search_entities': self._public_search_entities(body)}
        allowed = body.get('allow_background_check', False)
        if type(allowed) is not bool:
            fail('INVALID_ARGUMENT', 400)
        allowance = body.get('background_check_allowance')
        if allowed and allowance is None:
            allowance = {'max_checks':1, 'limits':dict(self.run_limits)}
        if allowance is not None:
            if (not isinstance(allowance, dict) or set(allowance) != {'max_checks','limits'}
                    or type(allowance['max_checks']) is not int or allowance['max_checks'] != 1):
                fail('INVALID_ARGUMENT', 400)
            result['background_check_allowance'] = {'max_checks': 1,
                'limits': limits(allowance['limits'], self.run_limits)}
        return result

    def _public_search_entities(self, body):
        values = body.get('public_search_entities', [])
        if not isinstance(values, list) or len(values) > 32:
            fail('INVALID_ARGUMENT', 400)
        result, seen = [], set()
        for value in values:
            if not isinstance(value, str) or len(value) > 200:
                fail('INVALID_ARGUMENT', 400)
            name = unicodedata.normalize('NFC', value).strip()
            if not name or name.casefold() in seen:
                fail('INVALID_ARGUMENT', 400)
            seen.add(name.casefold())
            result.append(name)
        return result

    def _create_goal(self, trip_id, token, body, revision):
        objective = text(body.get('objective') or body.get('brief'))
        return self.store.create_goal(trip_id, token, objective,
            body.get('success_criteria', [objective]), self._constraints(body),
            {'tools': sorted(self.bindings.gateway.handlers), 'limits': self.run_limits}, revision)

    def _enqueue(self, db, goal, event, key):
        self._model_guard()
        return self.store._start_authorized_run(db, goal, event, self.run_limits, key)

    def create(self, body):
        token = body.get('client_token')
        if not isinstance(token, str) or not re.fullmatch('[a-fA-F0-9]{64}', token):
            fail('CLIENT_TOKEN_REQUIRED', 400)
        clean = {k:v for k,v in body.items() if k != 'client_token'}
        scope = hashlib.sha256(token.encode()).hexdigest()
        key = self._key(scope, 'create', clean)
        with self.store.transaction() as db:
            replay = self._replay(db, key, clean)
            if replay is not None:
                self.store._trip(db, replay['trip']['id'], token)
                return dict(replay, token=token)
            self._model_guard()
            brief = text(body.get('brief'))
            evidence = body.get('evidence_text', '')
            if not isinstance(evidence, str) or len(evidence) > 20000 or len(brief)>6000:
                fail('INVALID_ARGUMENT', 400)
            additions = parse_materials(body)
            trip = self.trips._new_trip(brief, evidence, [], token,
                links=additions['links'], documents=additions['documents'])
            db.execute('INSERT INTO trips VALUES (?,?)', (trip['id'], canonical(trip)))
            goal = self._create_goal(trip['id'], token, body, trip['revision'])
            run = self._enqueue(db, goal, {'type':'goal_created','request':goal['objective']}, key)
            result = self._receipt(db, key, trip['id'], clean,
                                   dict(trip=public_trip(trip), goal=goal, run=run,
                                        material_receipts=material_receipts(trip['material_sources'])))
        self.worker.notify()
        return dict(result, token=token)

    def mutate_trip(self, trip_id, token, action, body, object_id=None):
        with self.store.transaction() as db:
            trip = self.store._trip(db, trip_id, token)
            key = self._key(trip_id, [action, object_id], body)
            replay = self._replay(db, key, body)
            if replay is not None:
                return replay
            if action in {'apply','undo'}:
                result = self._command(db, trip, token, action, body, key)
            else:
                self._model_guard()
                result = self._mutate(db, trip, token, action, body, object_id, key)
            self._receipt(db, key, trip_id, body, result)
        self.worker.notify()
        return result

    def _mutate(self, db, trip, token, action, body, object_id, key):
        trip_id = trip['id']
        if action == 'goals':
            check_revision(trip, body)
            receipts = append_materials(trip, body, key)
            self.trips._save(db, trip)
            goal = self._create_goal(trip_id, token, body, body.get('expected_revision'))
            result = {'goal':goal, 'material_receipts':receipts}
            event = dict(type='goal_created', request=goal['objective'])
        else:
            goal = self._goal(db, trip_id)
            if action == 'materials':
                source = self.workspace.ingest_material(trip_id, token, body.get('text'), body.get('source_id'))
                result, event = {'source':source}, dict(type='material_changed', source_id=source['source_id'])
            elif action == 'retract':
                source = self.workspace.retract_material(trip_id, token, object_id)
                result, event = {'source':source}, dict(type='material_retracted', source_id=object_id)
            elif action == 'answer':
                pending = self.workspace._get(db, trip_id, 'input', object_id)
                goal = self.store._goal_for_user(db, pending['goal_id'], token)
                answered = self.workspace.answer_input(trip_id, token, object_id, body.get('answer'))
                result, event = {'input':answered}, dict(type='input_answered', input_id=object_id)
            elif action == 'corrections':
                correction = self.workspace.record_correction(trip_id, token, body.get('content'))
                goal = self._correct_goals(db, trip_id, token, goal['id'], correction)
                result, event = {'correction':correction}, dict(type='user_correction', correction_id=correction['id'])
            else:
                fail('NOT_FOUND',404)
        result['run'] = self._enqueue(db, goal, event, key)
        return result

    def mutate_goal(self, goal_id, token, action, body):
        with self.store.transaction() as db:
            goal = self.store._goal_for_user(db, goal_id, token)
            key = self._key(goal_id, action, body)
            replay = self._replay(db, key, body)
            if replay is not None:
                return replay
            if action == 'events':
                if goal.get('command_only'):
                    fail('COMMAND_ONLY_GOAL', 403)
                request = text(body.get('request'))
                trip = self.store._trip(db, goal['trip_id'], token)
                check_revision(trip, body)
                receipts = append_materials(trip, body, key)
                self.trips._save(db, trip)
                result = {'run':self._enqueue(db, goal,
                    dict(type='user_request', request=request), key), 'material_receipts':receipts}
            else:
                status = {'confirm':'completed','cancel':'cancelled'}[action]
                goal = self.store.update_goal(goal_id, token, body.get('expected_goal_version'), status=status)
                self._cancel_obsolete(db, goal)
                result = {'goal':goal}
            self._receipt(db, key, goal['trip_id'], body, result)
        self.worker.notify()
        return result

    def _correct_goals(self, db, trip_id, token, selected_id, correction):
        selected = None
        entry = {'id':correction['id'], 'content':correction['content'], 'source':'explicit_user',
                 'created_at':correction['created_at']}
        for row in db.execute('SELECT data FROM autopilot_goals').fetchall():
            goal = json.loads(row[0])
            if goal['trip_id'] != trip_id or goal['status'] != 'active':
                continue
            constraints = dict(goal['constraints'])
            constraints['user_corrections'] = constraints.get('user_corrections', []) + [entry]
            goal = self.store.update_goal(goal['id'], token, goal['version'], constraints=constraints)
            self._cancel_obsolete(db, goal)
            if goal['id'] == selected_id:
                selected = goal
        return selected

    def _cancel_obsolete(self, db, goal):
        for row in db.execute('SELECT data FROM autopilot_runs WHERE goal_id=?', (goal['id'],)).fetchall():
            run = json.loads(row[0])
            if run['status'] in {'pending','running'} and run['goal_version'] != goal['version']:
                run.update(status='cancelled', finished_at=self.store.clock(),
                    result={'outcome_status':'blocked', 'error_code':'GOAL_VERSION_CONFLICT',
                            'user_summary':'用户已更新或结束目标，本轮已停止。'})
                self.store._save(db, 'autopilot_runs', run)

    def _command(self, db, trip, token, action, body, key):
        name = {'apply':'apply_revision','undo':'undo_revision'}[action]
        refkey = 'proposal_id' if action == 'apply' else 'revision_id'
        args = {refkey:text(body.get(refkey)), 'expected_revision':body.get('expected_revision')}
        # Resolve the original goal rather than a newer, unrelated active goal.
        table = 'autopilot_proposals' if action == 'apply' else 'autopilot_revisions'
        record = self.bindings.revisions._row(db, table, args[refkey], trip['id'])
        if action == 'undo':
            record = self.bindings.revisions._row(db, 'autopilot_proposals', record['proposal_id'], trip['id'])
        goal = self.store._goal_for_user(db, record['dependencies']['goal_id'], token)
        if action == 'undo' and goal['status'] != 'active':
            goal = self._undo_goal(trip, token, goal, args)
        run = self.store._start_authorized_run(db, goal,
            {'type':'user_command', 'server_command':{'name':name,'arguments':args}}, self.run_limits, key)
        lease = self.store.claim_run(run['id'], 'user-command-'+uuid4().hex)
        receipt = self.bindings.gateway.execute(run['id'], lease,
            {'id':'explicit-command','type':'function','function':{'name':name,'arguments':canonical(args)}})
        run = self.store.finish_run(run['id'], lease,
            'completed' if receipt['status']=='succeeded' else 'failed', {'receipt':receipt})
        if goal.get('command_only') and receipt['status'] == 'succeeded':
            self.store.update_goal(goal['id'], token, goal['version'], status='completed')
        return dict(run=run, receipt=receipt, trip=public_trip(self.trips._load(db, trip['id'])))

    def _undo_goal(self, trip, token, original, arguments):
        # A closed goal did not receive later trip corrections. Copying its old
        # constraints alone would manufacture a matching dependency hash.
        with self.store.transaction() as db:
            current = {row['id'] for row in self.workspace._rows(db, trip['id'], 'memory')
                if row.get('memory_kind') == 'explicit_correction' and row.get('status') == 'active'
                and self.workspace._valid(trip, row)
                and (row.get('expires_at') is None or row['expires_at'] > self.store.clock())}
        known = {row['id'] for row in original['constraints'].get('user_corrections', [])}
        if current != known:
            fail('UNDO_REVIEW_REQUIRED')
        goal = self.store.create_goal(trip['id'], token,
            '撤销指定修改：' + arguments['revision_id'],
            ['逆转指定修改且保留后续不相关编辑；存在冲突则不应用'], original['constraints'],
            {'tools':['read_trip','undo_revision'], 'limits':self.run_limits},
            arguments['expected_revision'])
        goal.update(command_only=True, undo_revision_id=arguments['revision_id'])
        with self.store.transaction() as db:
            self.store._save(db, 'autopilot_goals', goal)
        return goal
