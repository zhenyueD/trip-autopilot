"""Authenticated public projections; native context and credentials stay private."""
import json
import math
from .autopilot_advice import advice_integrity, advice_matches_state
from .autopilot_contract import fingerprint, public_run
from .autopilot_revision_fields import changes
from .autopilot_revision_guards import assert_dependencies, dependency_snapshot
from .autopilot_workspace_records import material_state
from .store import StateError, public_trip
from .uncertainty_contract import UncertaintyError

HIDDEN = frozenset({'token_hash', 'token_sha256', 'lease_secret', 'claim_secret',
    'lease_worker', 'lease_until', 'request_hash', 'reasoning_content', 'reasoning',
    'private_reasoning', 'native', 'messages', 'review', 'review_sha256', 'dependencies'})


def sanitize(value):
    if isinstance(value, dict):
        return {k:sanitize(v) for k,v in value.items() if k not in HIDDEN}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    return value


def _advice_source_ids(advice):
    """Rejected candidates can still contain declared sources inside claims."""
    claims = advice.get('claims')
    declarations = [advice] + (claims if isinstance(claims, list) else [])
    return {id_ for row in declarations if isinstance(row, dict)
            and isinstance(row.get('source_ids'), list)
            for id_ in row['source_ids'] if isinstance(id_, str)}


class ViewMethods:
    def state(self, trip_id, token):
        with self.store.transaction() as db:
            trip = self.store._trip(db, trip_id, token)
            goals = [json.loads(r[0]) for r in db.execute('SELECT data FROM autopilot_goals ORDER BY rowid DESC')]
            goals = [g for g in goals if g['trip_id'] == trip_id]
            rows = [json.loads(r[0]) for r in db.execute('SELECT data FROM autopilot_runs ORDER BY rowid DESC')]
            runs = [public_run(r, self.store.clock()) for r in rows if r['trip_id'] == trip_id]
            run_ids = {r['id'] for r in runs}
            operations = [json.loads(r[0]) for r in db.execute('SELECT data FROM autopilot_operations ORDER BY rowid DESC')]
            operations = [r for r in operations if r['run_id'] in run_ids]
            stale_runs, expired_runs = self._project_advice_results(db, operations, trip, {r['id']: r for r in rows})
            for run in runs:
                if run['id'] in stale_runs:
                    run['result'] = {'outcome_status':'partial', 'error_code':'STALE_SOURCE_RESULT',
                        'user_summary':'相关材料或核验时效已变更，旧结论需重新核对。'}
                elif run['id'] in expired_runs and isinstance(run.get('result'), dict):
                    run['result'] = dict(run['result'], verification_status='expired')
            workspace = self.workspace.snapshot(trip_id, token)
            workspace['memories'] = workspace.pop('memory')
            for key in workspace:
                workspace[key].reverse()
            proposals = self._proposals(db, trip)
            revisions = [json.loads(r[0]) for r in db.execute(
                'SELECT data FROM autopilot_revisions WHERE trip_id=? ORDER BY rowid DESC', (trip_id,))]
            revisions = [dict({k:v for k,v in r.items() if k not in {'before','after'}},
                can_undo=r['kind'] == 'apply' and r.get('before') is not None,
                undo_unavailable_reason=('NOT_APPLY_REVISION' if r['kind'] != 'apply' else
                    'NO_PREVIOUS_PLAN' if r.get('before') is None else None)) for r in revisions]
            return sanitize(dict(trip=public_trip(trip), goals=goals, runs=runs,
                operations=operations, workspace=workspace, proposals=proposals,
                revisions=revisions, capabilities=self.capabilities()))

    def _hide_stale_results(self, operations, trip):
        stale_runs = set()
        current = material_state(trip, self.store.clock())
        for operation in operations:
            result = operation.get('result')
            advice = operation.get('arguments', {}).get('advice')
            hidden_source = isinstance(advice, dict) and any(not any(
                source.get('id') == id_ and self.workspace._visible(source)
                for source in trip.get('material_sources', [])) for id_ in _advice_source_ids(advice))
            changed_material = isinstance(result, dict) and result.get('material_state', current) != current
            if hidden_source or changed_material:
                operation['result'] = {'status':'invalidated', 'error_code':'STALE_SOURCE_RESULT'}
                if isinstance(advice, dict):
                    operation['arguments'] = {'advice': {'status': 'invalidated'}}
                stale_runs.add(operation['run_id'])
        return stale_runs

    def _project_advice_results(self, db, operations, trip, runs):
        stale_runs, expired_runs = self._hide_stale_results(operations, trip), set()
        for operation in operations:
            result = operation.get('result')
            if not isinstance(result, dict) or result.get('scope') != 'advice':
                continue
            run = runs[operation['run_id']]
            if self.bindings.resolve_outcome(db, run, trip, result.get('advice_id')):
                continue
            if self._advice_only_expired(db, operation, run, trip):
                operation['result'] = dict(result, verdict='expired', historical_verdict=result.get('verdict'),
                                           verification_current=False)
                expired_runs.add(operation['run_id'])
            else:
                operation['result'] = {'status':'invalidated', 'error_code':'STALE_SOURCE_RESULT'}
                operation['arguments'] = {'advice': {'status': 'invalidated'}}
                stale_runs.add(operation['run_id'])
        return stale_runs, expired_runs

    def _advice_only_expired(self, db, operation, run, trip):
        """Keep historical text only when every binding except receipt time is current."""
        result, now = operation['result'], self.store.clock()
        until = result.get('valid_until')
        if (type(until) not in (int, float) or not math.isfinite(until) or until > now
                or not advice_integrity(operation) or not advice_matches_state(result, run, trip)):
            return False
        goal = self.store._load(db, 'autopilot_goals', run['goal_id'])
        if (result.get('dependency_sha256') != fingerprint(dependency_snapshot(trip, goal, db))
                or result.get('user_state_sha256') != fingerprint(self.workspace.context_user_state(db, trip))):
            return False
        return all(self.bindings.resolve_outcome(db, run, trip, target)
            for binding in result.get('semantic', {}).get('evidence_bindings', {}).values()
            for target in binding.get('target_refs', []))

    def _proposals(self, db, trip):
        result = []
        rows = db.execute('SELECT data FROM autopilot_proposals WHERE trip_id=? ORDER BY rowid DESC', (trip['id'],)).fetchall()
        for row in rows:
            proposal = json.loads(row[0])
            view = {k:proposal[k] for k in ('id','trip_id','expected_revision','valid_until','created_at')}
            try:
                goal = self.store._load(db, 'autopilot_goals', proposal['dependencies']['goal_id'])
                if goal['status'] != 'active' or trip['revision'] != proposal['expected_revision']:
                    raise StateError('STALE_PROPOSAL', '已失效', 409)
                self.bindings.revisions._baseline(db, trip, proposal)
                assert_dependencies(proposal, trip, goal, self.bindings.revisions.records, db, self.store.clock())
                view.update(status='proposed', plan=proposal['plan'],
                            changes=changes(proposal['previous'], proposal['plan']))
            except (StateError, UncertaintyError) as error:
                view.update(status='invalidated', error_code=error.code)
            result.append(view)
        return result
