"""Opt-in request-acceptance deadline and transaction fencing for CoreStudio.

No defaults, flags or global service instances are changed here. Parent runtime
installs this pool explicitly. Daemon callbacks can outlive a request, but all
clone-store transactions check the same sealed budget before committing.
"""
from concurrent.futures import Future
from contextlib import contextmanager
import copy
import hashlib
import sqlite3
import threading
import time

from .execution_budget import BudgetCallResult, ExecutionBudget, run_with_deadline
from .phase1_budget_context import bind_budget, bind_model_caller, uncharged_caller, use_budget
from .phase1_revision import Phase1RevisionReview
from .store import StateError, TripStore, public_trip as stored_public_trip


class _BudgetStoreMixin:
    def _check_budget(self):
        if not self._phase1_budget.can_publish():
            raise StateError('EXECUTION_BUDGET', '本轮执行预算已结束，迟到结果不能覆盖已返回状态', 409)

    def _review_delivery(self, trip_id, revision, plan, metrics, existing=None):
        from .blind_spots import _critic_result, validate_blind_spot_review
        from .phase1_revision import apply_candidate_review
        from .writer_critic import mechanical_diff, plan_hash
        from .validation import PlanError
        self._check_budget()
        trip = self.internal_get(trip_id)
        if trip['revision'] != revision or trip['status'] != 'running':
            raise StateError('STALE_JOB', '任务版本已变化', 409)
        diff = mechanical_diff(trip.get('plan'), plan)
        candidate_hash, diff_hash = plan_hash(plan), plan_hash(diff)
        if (isinstance(existing, dict) and existing.get('verdict') == 'pass'
                and existing.get('candidate_sha256') == candidate_hash
                and existing.get('diff_sha256') == diff_hash
                and validate_blind_spot_review(existing.get('blind_spots'))['structurally_complete']
                and _critic_result(existing.get('sixth_check'), existing['blind_spots'])['verdict'] == 'pass'):
            return existing
        hook = getattr(self, '_phase1_candidate_review', None)
        if hook is None:
            raise PlanError('Phase 1交付缺少盲区语义审查，已有方案保留')
        payload = {'candidate': copy.deepcopy(plan), 'mechanical_diff': diff,
                   'traveler_profile': copy.deepcopy(trip.get('traveler_profile') or {})}
        result = apply_candidate_review(hook, payload, candidate_hash, diff_hash, metrics, {})
        if result['verdict'] != 'pass':
            from .initial_blind_delivery import unchecked_initial_delivery
            if trip.get('plan') is None:
                unchecked = unchecked_initial_delivery(trip, plan, metrics, result)
                if unchecked is not None:
                    return unchecked
            raise PlanError('交付前盲区检查未完成：' + '；'.join(result['objections']))
        return result

    def update_job(self, trip_id, revision, **fields):
        if fields.get('plan') is not None and fields.get('status') == 'ready':
            metrics = fields.setdefault('metrics', {})
            result = self._review_delivery(trip_id, revision, fields['plan'], metrics)
            metrics['phase1_delivery_review'] = copy.deepcopy(result)
        if fields.get('status') in ('ready', 'needs_attention', 'error', 'failed'):
            fields.setdefault('metrics', {})['execution_budget'] = self._phase1_budget._root.snapshot()
        return super().update_job(trip_id, revision, **fields)

    def commit_reviewed_plan(self, trip_id, revision, plan, metrics, review, **kwargs):
        result = self._review_delivery(trip_id, revision, plan, metrics,
                                      existing=(review or {}).get('phase1_review'))
        receipt = {**review, 'phase1_review': copy.deepcopy(result)}
        metrics['execution_budget'] = self._phase1_budget._root.snapshot()
        return super().commit_reviewed_plan(trip_id, revision, plan, metrics, receipt, **kwargs)

    @contextmanager
    def connection(self):
        try:
            # Ancestor closure and actual COMMIT share one lock. SQLite never
            # waits for another writer while holding this publication fence.
            with self._phase1_budget.publication_guard():
                with _immediate_store(self._phase1_base_store).connection() as db:
                    db.set_progress_handler(lambda: 0 if self._phase1_budget.can_publish() else 1, 500)
                    try:
                        yield db
                        self._check_budget()
                    except BaseException:
                        db.set_progress_handler(None, 0)
                        raise
        except TimeoutError:
            raise StateError('EXECUTION_BUDGET', '本轮执行预算已结束，迟到结果不能覆盖已返回状态', 409) from None


def guard_store(store, budget):
    """Preserve TripStore subclasses without re-running schema/migration init."""
    if not isinstance(store, TripStore):
        raise TypeError('Phase 1 requires a TripStore-compatible transaction boundary')
    guarded_type = type('Phase1' + type(store).__name__, (_BudgetStoreMixin, type(store)), {})
    guarded = object.__new__(guarded_type)
    guarded.__dict__.update(store.__dict__)
    guarded._phase1_base_store, guarded._phase1_budget = store, budget
    return guarded


class _NoopSlots:
    def release(self):
        # The watchdog owns the real release, including queued/time-out jobs.
        return None


def _clone_service(service, budget, trip):
    from . import model
    clone = copy.copy(service)
    clone.store, clone.slots = guard_store(service.store, budget), _NoopSlots()
    plan = trip.get('plan') or {}
    days = [day for day in plan.get('days', []) if isinstance(day, dict)]
    day_keys = {str(index): 'day-' + str(day.get('day', index + 1)) for index, day in enumerate(days)}
    clone.phase1_item_days = {item['id']: day_keys[str(index)] for index, day in enumerate(days)
                            for item in day.get('items', []) if isinstance(item, dict) and item.get('id')}
    clone.phase1_budget = budget
    clone.caller = bind_model_caller(service.caller, budget, day_keys)
    for name in ('link_reader', 'refresher', 'preparation_builder', 'route_builder', 'place_localizer'):
        function = getattr(service, name, None)
        if callable(function):
            setattr(clone, name, bind_budget(function, budget))
    researcher = getattr(service, 'researcher', None)
    if researcher is not None:
        clone.researcher = copy.copy(researcher)
        if hasattr(researcher, 'reader'):
            clone.researcher.reader = clone.link_reader
    raw = model.call_json if service.caller in (model.call_json, model.call_planner_json) else service.caller
    destination = (trip.get('preview') or {}).get('destinations') or plan.get('destinations')
    context = {'destination': destination, 'dates': [day['date'] for day in days if day.get('date')],
               'day_keys': list(day_keys.values())}
    from .phase1_lookup import Phase1SourceResolver
    resolver = getattr(service, 'phase1_blind_spot_resolver', None) or Phase1SourceResolver(trip,
        trip_loader=lambda: clone.store.internal_get(trip['id']))
    context['source_summaries'] = getattr(resolver, 'sources', [])
    clone.phase1_candidate_review = Phase1RevisionReview(caller=uncharged_caller(raw), budget=budget,
        resolver=resolver, context=context)
    clone.store._phase1_candidate_review = clone.phase1_candidate_review
    return clone


class _ImmediateStoreMixin:
    @contextmanager
    def connection(self):
        # Do not enter TripStore.connection: even its journal-mode PRAGMA can
        # wait for the original 15-second SQLite timeout before yielding.
        db = sqlite3.connect(str(self.path), timeout=0)
        try:
            db.execute('PRAGMA secure_delete=ON')
            with db:
                yield db
        finally:
            db.close()


def _immediate_store(store):
    result = object.__new__(type('Immediate' + type(store).__name__,
                                (_ImmediateStoreMixin, type(store)), {}))
    result.__dict__.update(store.__dict__)
    return result


_TERMINAL_ERROR = '本次执行窗口已结束，可重试继续未完成内容。'


class Phase1Pool:
    def __init__(self, service, underlying_pool, *, budget_factory=ExecutionBudget):
        self.service, self.underlying = service, underlying_pool
        self.budget_factory = budget_factory
        self._lock, self._budgets, self._requests = threading.RLock(), {}, {}
        self._snapshots, self._terminals, self._recoveries = {}, {}, {}
        self._pending, self._finalizers, self._forgotten = {}, set(), set()
        self._store = _immediate_store(service.store)

    @staticmethod
    def _prior_execution(trip, marker, budget=None):
        prior = {key: copy.deepcopy(value) for key, value in marker.items()
                 if key != 'prior_executions'}
        saved = (trip.get('metrics') or {}).get('execution_budget') or {}
        snapshot = prior.get('execution_budget') or {}
        if budget is not None:
            prior['execution_budget'] = budget.snapshot()
        elif (saved.get('started_at') == snapshot.get('started_at')
                and saved.get('deadline') == snapshot.get('deadline')
                and saved.get('calls_used', 0) > snapshot.get('calls_used', 0)):
            prior['execution_budget'] = copy.deepcopy(saved)
        if budget is None and not marker.get('usage_complete'):
            prior['usage_complete'] = False
        return prior

    def _request_budget(self, trip, revision, digest, proposed):
        marker = trip.get('phase1_execution_marker') or {}
        root = self._requests.get((trip['id'], digest))
        previous = marker.get('request_sha256') == digest
        retry = trip.get('revision_retry') or {}
        explicit = retry.get('revision') == revision and retry.get('explicit') and retry.get('same_request')
        if previous and marker.get('accepted_revision', 0) >= revision:
            raise StateError('ALREADY_DISPATCHED', '本次重试已接收，请等待结果', 409)
        reason = (root.snapshot()['reason'] if root is not None else 'restart') if previous else ''
        if previous and (not explicit or reason):
            if not explicit:
                proposed.close('restart_recovery_required')
                return proposed, marker
            prior = self._prior_execution(trip, marker, root)
            prior['recovery_reason'] = reason
            root = proposed
            marker = {'prior_executions': copy.deepcopy(marker.get('prior_executions') or []) + [prior]}
        elif not previous:
            root, marker = proposed, {}
        marker.update(request_sha256=digest, accepted_revision=revision,
                      execution_budget=root.snapshot(), usage_complete=False)
        trip['phase1_execution_marker'] = marker
        return root, marker

    def _accept(self, trip_id, revision, proposed):
        # Publish the in-memory aliases only AFTER the marker commits. A busy
        # COMMIT must not leave an unpersisted root available to later retries.
        with self._lock:
            with self._store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                trip = self._store._load(db, trip_id)
                if trip['revision'] != revision or trip['status'] != 'running':
                    raise StateError('STALE_JOB', '任务版本已变化', 409)
                identity = str(trip.get('revision_request_id') or f'initial:{revision}')
                trip.setdefault('revision_request_id', identity)
                digest = hashlib.sha256(identity.encode()).hexdigest()
                request_key, revision_key = (trip_id, digest), (trip_id, revision)
                root, marker = self._request_budget(trip, revision, digest, proposed)
                self._store._save(db, trip)
            budget = root.attempt(revision)
            self._requests[request_key] = root
            self._budgets[revision_key] = budget
            if marker.get('prior_executions'):
                self._recoveries[revision_key] = copy.deepcopy(marker['prior_executions'])
            self._snapshots[trip_id] = copy.deepcopy(trip)
            return budget

    def submit(self, function, /, *args, **kwargs):
        if (getattr(function, '__self__', None) is not self.service
                or getattr(function, '__name__', '') not in ('_run', '_run_preparation')):
            return self.underlying.submit(function, *args, **kwargs)
        trip_id, revision = args[:2]
        proposed = self.budget_factory()  # Acceptance time includes all queue work.
        try:
            budget = self._accept(trip_id, revision, proposed)
        except sqlite3.OperationalError:
            # Cannot establish durable ownership: no physical work is started.
            proposed.close('acceptance_storage_unavailable')
            budget = proposed
            with self._lock:
                self._budgets[(trip_id, revision)] = budget
        future = Future()
        future.set_running_or_notify_cancel()
        queued = self.underlying.submit(self._execute, function, args, kwargs, budget)
        def supervise():
            result, failure = None, None
            try:
                result = run_with_deadline(queued.result, budget)
                if result.status != 'ok':
                    queued.cancel()
                self._record_completion(trip_id, revision, budget, result.status)
            except Exception as error:
                failure = error
            finally:
                self.service.slots.release()
            if failure is not None:
                future.set_exception(failure)
            else:
                future.set_result(result)
        try:
            threading.Thread(target=supervise, name='phase1-request-deadline', daemon=True).start()
        except RuntimeError:
            queued.cancel()
            budget.close('supervisor_start_failed')
            raise
        return future

    def _execute(self, function, args, kwargs, budget):
        if not budget.can_publish():
            raise StateError('EXECUTION_BUDGET', '排队期间预算已结束，未启动任务', 409)
        trip = self.service.store.internal_get(args[0])
        clone = _clone_service(self.service, budget, trip)
        with use_budget(budget):
            function.__func__(clone, *args, **kwargs)
            completed = clone.store.internal_get(args[0])
            if completed['revision'] != args[1] or completed['status'] != 'ready':
                return BudgetCallResult('error', reason='workflow_incomplete')
            return BudgetCallResult('ok')

    def _record_completion(self, trip_id, revision, budget, outcome):
        key = (trip_id, revision)
        with self._lock:
            record = {'execution_budget': budget._root.snapshot(), 'execution_outcome': outcome,
                      'execution_attempt': budget.snapshot(),
                      'terminal_persistence': 'pending'}
            if key in self._recoveries:
                record['prior_executions'] = copy.deepcopy(self._recoveries[key])
            if budget._root.snapshot()['closed'] == 'restart_recovery_required':
                record.update(execution_outcome='restart_recovery_required', usage_complete=False)
            self._pending[key] = record
            if outcome != 'ok':
                self._terminals[key] = record
        # This single attempt never waits for a SQLite lock. Durability can
        # lag, but the memory projection, slot and Future never wait for it.
        if not self._finalize(key):
            self._defer(key)

    def _finalize(self, key):
        with self._lock:
            record = copy.deepcopy(self._pending.get(key))
        if record is None:
            return True
        try:
            with self._store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                trip = self._store._load(db, key[0])
                if trip.get('revision') == key[1]:
                    self._save_completion(db, trip, record)
                snapshot = copy.deepcopy(trip)
        except sqlite3.OperationalError:
            return False
        except StateError:
            snapshot = None  # Deleted trips must never be resurrected.
        with self._lock:
            self._pending.pop(key, None)
            terminal = self._terminals.get(key)
            if terminal is not None:
                terminal['terminal_persistence'] = 'persisted'
            if snapshot is not None and key[0] not in self._forgotten:
                old = self._snapshots.get(key[0])
                if old is None or snapshot['revision'] >= old['revision']:
                    self._snapshots[key[0]] = snapshot
        return True

    def _save_completion(self, db, trip, record):
        if trip['status'] == 'running' and record['execution_outcome'] != 'ok':
            trip.update(status='needs_attention', error=self._terminal_error(record, trip))
            event = '本轮已停止等待；原方案保留，可重试继续' if trip.get('plan') else '本轮已停止等待；尚未生成完整方案，可重试继续'
            trip['events'] = (trip.get('events', []) + [{'label': event,
                'at': self._store_time()}])[-24:]
            self._store._park_legacy_materials(trip)
            self._store._finish_revision(trip)
        trip.setdefault('metrics', {}).update(record, terminal_persistence='persisted')
        self._store._record_failure(trip)
        marker = trip.get('phase1_execution_marker')
        if marker and record['execution_outcome'] != 'restart_recovery_required':
            marker.update(execution_budget=copy.deepcopy(record['execution_budget']),
                          usage_complete=not any(call['status'] == 'running'
                              for call in record['execution_budget']['calls']))
        self._store._save(db, trip)

    @staticmethod
    def _terminal_error(record, trip):
        if record['execution_outcome'] == 'restart_recovery_required':
            reason = '执行进程已重启，请点重试继续本次请求。'
        else:
            reason = _TERMINAL_ERROR
        retry = trip.get('revision_retry') or {}
        previous = trip.get('error') or ((trip.get('last_failure') or {}).get('error')
                                       if retry.get('same_request') else None)
        retained = '已有方案保留。' if trip.get('plan') else '尚未生成完整方案。'
        return (previous + '；' if previous else '') + reason + retained

    @staticmethod
    def _store_time():
        from .store import now
        return now()

    def _defer(self, key):
        with self._lock:
            if key in self._finalizers or key not in self._pending:
                return
            self._finalizers.add(key)
        def finish():
            try:
                until = time.monotonic() + 30
                while time.monotonic() < until:
                    if self._finalize(key):
                        return
                    time.sleep(.05)
            finally:
                with self._lock:
                    self._finalizers.discard(key)
        threading.Thread(target=finish, name='phase1-terminal-persistence', daemon=True).start()

    def public_trip(self, trip_id, token):
        """Authenticated terminal projection, even while SQLite is locked."""
        stale = False
        try:
            trip = self._store.internal_get(trip_id)
        except sqlite3.OperationalError:
            with self._lock:
                trip = None if trip_id in self._forgotten else copy.deepcopy(self._snapshots.get(trip_id))
            if trip is None:
                raise StateError('STATE_BUSY', '状态暂时不可读，请稍后刷新', 503) from None
            stale = True
        self._store._authorize(trip, token)
        key = (trip_id, trip['revision'])
        with self._lock:
            terminal = copy.deepcopy(self._terminals.get(key))
        if terminal is not None and trip['status'] == 'running':
            trip.update(status='needs_attention', error=self._terminal_error(terminal, trip))
            self._store._finish_revision(trip)
            trip.setdefault('metrics', {}).update(terminal)
        if stale:
            trip.setdefault('metrics', {}).update(terminal_persistence='pending',
                                                 state_projection='cached')
        result = stored_public_trip(trip)
        result.pop('phase1_execution_marker', None)
        self._defer(key)
        return result

    def forget_trip(self, trip_id):
        """Call only after authenticated deletion succeeds."""
        with self._lock:
            self._forgotten.add(trip_id)
            self._snapshots.pop(trip_id, None)
            for table in (self._budgets, self._requests, self._terminals, self._pending,
                          self._recoveries):
                for key in [key for key in table if key[0] == trip_id]:
                    table.pop(key, None)

    def budget_snapshot(self, trip_id, revision):
        with self._lock:
            key = (trip_id, revision)
            budget = self._budgets.get(key)
            if budget is None:
                return None
            result = budget._root.snapshot()
            result['attempt'] = budget.snapshot()
            if key in self._recoveries:
                result.update(prior_execution=copy.deepcopy(self._recoveries[key][-1]),
                              prior_executions=copy.deepcopy(self._recoveries[key]))
            return result

    def shutdown(self, wait=True, *, cancel_futures=False):
        return self.underlying.shutdown(wait=wait, cancel_futures=cancel_futures)
