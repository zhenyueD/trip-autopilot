"""Durable checks. Trusted worker claims are not model-callable tools."""
import hmac
import secrets
from .autopilot_contract import canonical, duration, fail, integer, limits, text
from .autopilot_workspace_memory import timestamp
from .store import StateError


class JobMethods:
    def _background_allowance(self, goal):
        allowance = goal.get('constraints', {}).get('background_check_allowance')
        if not isinstance(allowance, dict) or set(allowance) != {'max_checks', 'limits'}:
            fail('BACKGROUND_NOT_AUTHORIZED', 403)
        try:
            count = integer(allowance['max_checks'], 1)
            approved = limits(allowance['limits'], goal['authorization']['limits'])
            if count > 100:
                fail('INVALID_ARGUMENT', 400)
        except StateError:
            fail('BACKGROUND_NOT_AUTHORIZED', 403)
        return count, approved

    def _schedule_check(self, db, run, trip, due_at, purpose, key, source_refs=None):
        timestamp(due_at)
        text(purpose)
        text(key)
        goal = self.store._load(db, 'autopilot_goals', run['goal_id'])
        max_checks, approved_limits = self._background_allowance(goal)
        refs = self._dependencies(trip,source_refs or [])
        request = dict(due_at=due_at,purpose=purpose,key=key,source_refs=refs)
        history = [row for row in self._rows(db, trip['id'], 'job')
                   if row['goal_id'] == run['goal_id']]
        for row in history:
            if row['key']==key:
                if canonical(row['request']) != canonical(request):
                    fail('IDEMPOTENCY_CONFLICT')
                if not self._job_valid(db,row):
                    fail('JOB_INVALIDATED')
                return dict(self._public(row),job_id=row['id'],next_run_at=row['due_at'])
        if len(history) >= max_checks:
            fail('BACKGROUND_ALLOWANCE_EXHAUSTED', 403)
        if not self.clock() < due_at <= self.clock()+30*86400:
            fail('INVALID_ARGUMENT',400)
        active = [r for r in self._rows(db,trip['id'],'job') if r['status'] in {'pending','claimed'}]
        if len(active)>=100:
            fail('JOB_LIMIT')
        row = self._new(trip,'job',run,**request,request=request,status='pending',
                        token_hash=trip['token_hash'],goal_version=run['goal_version'],
                        approved_run_limits=approved_limits,
                        claim_generation=0,claim_secret=None,claim_until=0,worker_id=None)
        self._put(db,row)
        return dict(self._public(row),job_id=row['id'],next_run_at=due_at)

    def _job_valid(self, db, row):
        try:
            trip = self.store.trip_store._load(db,row['trip_id'])
            goal = self.store._load(db,'autopilot_goals',row['goal_id'])
            _, approved_limits = self._background_allowance(goal)
            return (row.get('approved_run_limits') == approved_limits and self._valid(trip,row) and hmac.compare_digest(trip['token_hash'],row['token_hash']) and
                    goal['status']=='active' and goal['version']==row['goal_version'] and
                    'schedule_check' in goal['authorization']['tools'])
        except StateError:
            return False

    def _job(self, db, job_id):
        row = db.execute('SELECT trip_id FROM autopilot_workspace WHERE id=? AND kind=?',(job_id,'job')).fetchone()
        if not row:
            fail('ARTIFACT_NOT_FOUND',404)
        return self._get(db,row[0],'job',job_id)

    def due_jobs(self, limit=100):
        integer(limit,1)
        if limit>1000:
            fail('INVALID_ARGUMENT',400)
        with self.store.transaction() as db:
            rows = db.execute("SELECT id FROM autopilot_workspace WHERE kind='job'").fetchall()
            jobs = [self._job(db,r[0]) for r in rows]
            return [self._public(r) for r in sorted(jobs,key=lambda r:r['due_at'])
                    if r['status'] in {'pending','claimed'} and r['due_at']<=self.clock() and
                    r['claim_until']<=self.clock() and self._job_valid(db,r)][:limit]

    def claim_job(self, job_id, worker_id, lease_seconds=60):
        text(worker_id)
        duration(lease_seconds)
        with self.store.transaction() as db:
            row = self._job(db,job_id)
            if not self._job_valid(db,row):
                fail('JOB_INVALIDATED')
            if row['status'] not in {'pending','claimed'} or row['due_at']>self.clock():
                fail('JOB_NOT_DUE')
            if row['claim_until']>self.clock():
                fail('JOB_BUSY')
            row.update(status='claimed',worker_id=worker_id,claim_secret=secrets.token_hex(24),
                       claim_generation=row['claim_generation']+1,claim_until=self.clock()+lease_seconds)
            self._put(db,row)
            return dict(worker_id=worker_id,generation=row['claim_generation'],secret=row['claim_secret'])

    def settle_job(self, job_id, claim, status, result=None):
        if status not in {'succeeded','failed'}:
            fail('INVALID_ARGUMENT',400)
        with self.store.transaction() as db:
            row = self._job(db,job_id)
            if not self._job_valid(db,row):
                fail('JOB_INVALIDATED')
            if (not isinstance(claim,dict) or row['worker_id']!=claim.get('worker_id') or
                    row['claim_generation']!=claim.get('generation') or
                    not isinstance(claim.get('secret'),str) or
                    not hmac.compare_digest(row['claim_secret'] or '',claim['secret'])):
                fail('JOB_LEASE_LOST')
            if row['status']==status and row.get('result')==result:
                return self._public(row)
            if row['status']!='claimed' or row['claim_until']<=self.clock():
                fail('JOB_LEASE_LOST')
            canonical(result)
            row.update(status=status,result=result,settled_at=self.clock())
            self._put(db,row)
            return self._public(row)

    def cancel_job(self, trip_id, token, job_id):
        with self.store.transaction() as db:
            self.store._trip(db,trip_id,token)
            row = self._get(db,trip_id,'job',job_id)
            if row['status'] in {'succeeded','failed'}:
                fail('JOB_TERMINAL')
            row.update(status='cancelled',cancelled_at=self.clock())
            self._put(db,row)
            return self._public(row)
