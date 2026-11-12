"""A small durable worker: DB is the queue, not a browser tab or thread list."""
import hmac
import json
import logging
import threading
from uuid import uuid4

from .autopilot_contract import canonical
from .store import StateError

LOG = logging.getLogger(__name__)


class AutopilotWorker:
    def __init__(self, store, workspace, runner, *, poll_seconds=2):
        self.store, self.workspace, self.runner = store, workspace, runner
        self.poll_seconds = poll_seconds
        self.id = 'autopilot-' + uuid4().hex
        self.stopped, self.wake = threading.Event(), threading.Event()
        self.thread = None
        self._tick_lock = threading.Lock()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stopped.clear()
        self.thread = threading.Thread(target=self._loop, name='autopilot-worker', daemon=True)
        self.thread.start()

    def notify(self):
        self.wake.set()

    def stop(self):
        self.stopped.set(); self.wake.set()
        if self.thread:
            self.thread.join(timeout=1)

    def _loop(self):
        while not self.stopped.is_set():
            try:
                self.tick()
            except Exception:
                # Only error type/location goes to local logs; no prompts/tokens.
                LOG.error('Autopilot worker tick failed; durable work remains pending')
            self.wake.wait(self.poll_seconds)
            self.wake.clear()

    def _job_run(self, job_id, claim):
        with self.store.transaction() as db:
            row = self.workspace._job(db, job_id)
            if (not self.workspace._job_valid(db, row) or row['status'] != 'claimed'
                    or row['claim_until'] <= self.store.clock()
                    or row['worker_id'] != claim['worker_id']
                    or row['claim_generation'] != claim['generation']
                    or not hmac.compare_digest(row['claim_secret'] or '', claim['secret'])):
                raise StateError('JOB_LEASE_LOST', '检查任务授权或租约已失效', 409)
            goal = self.store._load(db, 'autopilot_goals', row['goal_id'])
            event = {'type': 'scheduled_check', 'job_id': job_id, 'request': row['purpose']}
            run = self.store._start_authorized_run(db, goal, event, row['approved_run_limits'], 'job:' + job_id)
            # Publish the queue entry and delivery receipt in one transaction.
            row.update(status='succeeded', result={'run_id': run['id'], 'meaning': 'enqueued_not_completed'},
                       settled_at=self.store.clock())
            self.workspace._put(db, row)
            return run

    def _expire_unleased(self):
        with self.store.transaction() as db:
            for row in db.execute('SELECT data FROM autopilot_runs').fetchall():
                run = json.loads(row[0])
                if run['status'] not in {'pending', 'running'}:
                    continue
                try:
                    self.store._current(db, run['id'])
                except StateError as error:
                    run.update(status='cancelled', finished_at=self.store.clock(),
                        result={'outcome_status': 'blocked', 'error_code': error.code,
                                'user_summary': '目标或授权已变化，旧工作已停止。'})
                    self.store._save(db, 'autopilot_runs', run)
                    continue
                if (run['deadline'] > self.store.clock()
                        or run['lease_until'] > self.store.clock()):
                    continue
                run.update(status='waiting', finished_at=self.store.clock(),
                    result={'outcome_status': 'partial', 'error_code': 'BUDGET_EXCEEDED',
                            'user_summary': '本轮时间额度已到，已有结果和原方案保留。',
                            'unresolved': ['执行中断后已超过原时间额度，需要新的授权事件继续。']})
                self.store._save(db, 'autopilot_runs', run)

    def tick(self):
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            self._expire_unleased()
            for job in self.workspace.due_jobs(limit=20):
                try:
                    claim = self.workspace.claim_job(job['id'], self.id)
                    self._job_run(job['id'], claim)
                except StateError:
                    continue  # Another worker, cancellation or changed source won.
            for run in self.store.list_runnable_runs(limit=20):
                if self.stopped.is_set():
                    return
                try:
                    self.runner.run(run['id'], self.id)
                except StateError:
                    continue  # Current worker/authorization fences are authoritative.
        finally:
            self._tick_lock.release()
