"""Trip-scoped durable workspace. Only execute is a model dispatch surface."""
import copy
import hashlib
import inspect
import json
from .autopilot_contract import canonical, fail, fingerprint, integer, text
from .detail_reviews import source_withdrawn
from .autopilot_workspace_records import RecordMethods, visible, material_state, source_freshness
from .autopilot_workspace_memory import MemoryMethods
from .autopilot_workspace_jobs import JobMethods


class AutopilotWorkspace(RecordMethods, MemoryMethods, JobMethods):
    tools = frozenset({'read_trip', 'read_source', 'update_work', 'request_input', 'retrieve_memory', 'propose_memory', 'schedule_check'})

    def __init__(self, store):
        self.store = store
        self.clock = store.clock
        with store.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS autopilot_workspace (id TEXT PRIMARY KEY, trip_id TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS autopilot_workspace_scope ON autopilot_workspace(trip_id,kind)")

    def execute(self, run_id, lease, call_id, name, arguments):
        if name not in self.tools or not isinstance(arguments, dict):
            fail('INVALID_ARGUMENT', 400)
        handler = getattr(self, '_' + name)
        try:
            inspect.signature(handler).bind(None, None, None, **arguments)
        except TypeError:
            fail('INVALID_ARGUMENT', 400)
        with self.store.transaction() as db:
            run = self.store._lease(db, run_id, lease)
            trip = self.store.trip_store._load(db, run['trip_id'])
            reserved = self.store.reserve_operation(run_id, lease, call_id, name, arguments, db=db)
            if reserved['disposition'] == 'replay':
                result = reserved['operation']['result']
                if result.get('material_state') != material_state(trip, self.clock()):
                    fail('STALE_OPERATION_RESULT')
                self._check_replay(db, trip, result, name)
                return result
            if reserved['disposition'] != 'execute':
                fail('OPERATION_UNKNOWN')
            result = handler(db, run, trip, **arguments)
            result['material_state'] = material_state(trip, self.clock())
            self.store.finish_operation(run_id, lease, reserved['operation']['id'], result, db=db)
            return result

    def _read_source(self, db, run, trip, source_id, start=0, end=None):
        text(source_id)
        integer(start)
        source = next((s for s in trip.get('material_sources', []) if s['id'] == source_id), None)
        if source is None or not self._visible(source):
            return dict(source_id=source_id, access_status='unknown', content=None,
                        reason='not_available_in_current_authorized_materials')
        content = source.get('content')
        if not isinstance(content, str) or not content:
            return dict(source_id=source_id, access_status='original_unavailable', content=None)
        end = min(len(content), start + 12000) if end is None else integer(end)
        if start > len(content) or end < start or end > len(content) or end-start > 12000:
            fail('INVALID_ARGUMENT', 400)
        return dict(source_id=source_id, access_status='available', content=content[start:end],
                    fingerprint=hashlib.sha256(content.encode()).hexdigest(),
                    locator=dict(source_id=source_id, start=start, end=end), total_chars=len(content),
                    version=source.get('workspace_version', 1), observed_at=source.get('observed_at'),
                    source_status=source.get('status'), url=source.get('url'),
                    **source_freshness(source, self.clock()))

    def _read_trip(self, db, run, trip, scope='summary', day_ids=None, fields=None):
        allowed = {'plan', 'brief', 'traveler_profile', 'intake', 'pending_materials',
                   'source_evidence', 'requirements', 'commitments', 'todos'}
        if (scope not in {'summary', 'full', 'days'} or
                fields is not None and (not isinstance(fields, list) or any(f not in allowed for f in fields)) or
                day_ids is not None and (not isinstance(day_ids, list) or
                    any(type(d) not in (str, int) for d in day_ids))):
            fail('INVALID_ARGUMENT', 400)
        stale = trip.get('workspace_stale_plan_hash') == fingerprint(trip.get('plan'))
        plan = None if stale else copy.deepcopy(trip.get('plan'))
        locked_ids = trip.get('locked_item_ids', [])
        locked = []
        def walk(value):
            if isinstance(value, dict):
                if value.get('id') in locked_ids:
                    locked.append(copy.deepcopy(value))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(plan)
        critical_keys = ('requirements', 'commitments', 'constraints', 'bookings',
                         'confirmed_bookings', 'hard_constraints', 'warnings', 'todos')
        critical = {} if stale else {k: copy.deepcopy(trip[k]) for k in critical_keys if k in trip}
        critical['plan'] = {k: copy.deepcopy(plan[k]) for k in critical_keys if isinstance(plan, dict) and k in plan}
        goal = self.context_goal(db, trip, self.store._load(db, 'autopilot_goals', run['goal_id']))
        user_state = self.context_user_state(db, trip)
        result = dict(trip_id=trip['id'], revision=trip['revision'], status=trip['status'],
                      locked_item_ids=locked_ids, locked_items=locked, critical_context=critical,
                      goal=goal, user_state=user_state, user_state_hash=fingerprint(user_state))
        result['requires_source_recheck'] = stale
        result['source_validity'] = [dict(source_id=s.get('id'), **source_freshness(s, self.clock()))
            for key in ('material_sources', 'source_evidence') for s in trip.get(key) or []
            if self._visible(s)]
        result['plan_state'] = fingerprint(trip.get('plan'))
        selected = fields or ['brief', 'plan', 'traveler_profile']
        for key in selected:
            result[key] = None if stale and key != 'traveler_profile' else copy.deepcopy(trip.get(key))
        if 'plan' in selected and isinstance(plan, dict):
            days = plan.get('days', [])
            if scope == 'summary':
                plan.pop('days', None)
                plan['day_index'] = [dict(day=d.get('day'), title=d.get('title')) for d in days]
            elif scope == 'days':
                if not day_ids:
                    fail('INVALID_ARGUMENT', 400)
                plan['days'] = [d for d in days if d.get('day') in day_ids]
                if len(plan['days']) != len(set(day_ids)):
                    fail('DAY_NOT_FOUND', 404)
            result['plan'] = plan
        return result

    def _check_replay(self, db, trip, result, name):
        if name == 'read_source' and result.get('content') is not None:
            source = next((s for s in trip.get('material_sources', [])
                           if s['id'] == result['source_id']), None)
            if source is None or not self._visible(source):
                fail('STALE_OPERATION_RESULT')
        if name == 'read_trip' and (result['revision'] != trip['revision'] or
                result['plan_state'] != fingerprint(trip.get('plan')) or
                result.get('user_state_hash') != fingerprint(self.context_user_state(db, trip))):
            fail('STALE_OPERATION_RESULT')
        if name == 'retrieve_memory' and result.get('as_of', self.clock()) > self.clock():
            fail('STALE_OPERATION_RESULT')
        records = result.get('items', []) if name == 'retrieve_memory' else [result]
        for row in records:
            if row.get('expires_at') is not None and row['expires_at'] <= self.clock():
                fail('STALE_OPERATION_RESULT')
            if row.get('kind') in {'input','work','memory','job'} and row.get('id'):
                current = self._get(db, trip['id'], row['kind'], row['id'])
                eligible = (self._context_eligible(trip, current, self.clock(), {'candidate', 'active'})
                            if row['kind'] == 'memory' else self._valid(trip, current))
                if not eligible or any(
                        current.get(key) != value for key,value in row.items()
                        if key in current and key not in {'token_hash','claim_secret'}):
                    fail('STALE_OPERATION_RESULT')

    def trusted_context_snapshot(self, run_id, lease):
        """Trusted current context without an extra model tool reservation."""
        with self.store.transaction() as db:
            run = self.store.assert_dispatch(run_id, lease, db=db)
            trip = self.store.trip_store._load(db, run['trip_id'])
            user_state = self.context_user_state(db, trip)
            return dict(trip=self._read_trip(db,run,trip), user_state=user_state,
                        work=[self._public(r) for r in self._rows(db,trip['id'],'work') if self._valid(trip,r)],
                        inputs=[self._public(r) for r in self._rows(db, trip['id'], 'input')
                                if self._context_eligible(trip, r, self.clock(), {'pending', 'answered'})],
                        materials=[dict(id=s['id'],kind=s.get('kind'),status=s.get('status'),
                                        chars=len(s.get('content','')),
                                        **source_freshness(s, self.clock())) for s in trip.get('material_sources',[])
                                   if self._visible(s)],
                        material_state=material_state(trip, self.clock()))
