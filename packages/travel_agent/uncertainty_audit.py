"""Human-only, reproducible weekly audit snapshots and a persistent breaker.

Authentication is supplied by the server, never from a model/user JSON field.
This service records an audit; it does not pretend to schedule one or perform it.
"""
from datetime import timedelta
import json
import random
from uuid import uuid4

from .uncertainty_contract import UncertaintyError, canonical, instant, require_text


def human_identity(actor):
    if (not isinstance(actor, dict) or actor.get('kind') != 'human'
            or actor.get('authenticated') is not True):
        raise UncertaintyError('HUMAN_REQUIRED', '人工审计必须由已认证的人类提交', 403)
    return require_text(actor.get('id'), 'reviewer_id')


class UncertaintyAudit:
    def __init__(self, store, *, rng=None):
        self.store = store
        self.rng = rng or random.SystemRandom()

    def control(self):
        with self.store._using() as db:
            return self.store._audit_control(db)

    def _save_control(self, db, value):
        db.execute('UPDATE uncertainty_audit_control SET data=? WHERE id=1', (canonical(value),))

    def _week(self):
        today = instant(self.store.clock()).date()
        return (today - timedelta(days=today.weekday())).isoformat()

    def sample_week(self):
        """Idempotent weekly entry point; invoke Monday from the server scheduler."""
        week = self._week()
        return self._sample(week, 'weekly:' + week)

    def sample_recheck(self):
        with self.store._using() as db:
            control = self.store._audit_control(db)
        repair = control.get('repair')
        if control['automatic_verified'] or not repair:
            raise UncertaintyError('REPAIR_REQUIRED', '停用后须先登记修复，再创建新人工批次', 409)
        return self._sample(self._week(), 'repair:' + repair['id'], repair=repair)

    def _sample(self, week, sampling_key, repair=None):
        with self.store._using(write=True) as db:
            old = db.execute('SELECT data FROM uncertainty_audit_batches WHERE sampling_key=?',
                             (sampling_key,)).fetchone()
            if old:
                return self._batch(db, json.loads(old[0])['id'])
            candidates = self._candidates(db, repair)
            selected = self.rng.sample(candidates, min(50, len(candidates)))
            batch = {'id': uuid4().hex, 'week': week, 'sampling_key': sampling_key,
                'created_at': self.store.clock(), 'population_size': len(candidates),
                'sample_size': len(selected), 'required_size': 50,
                'reviewed': 0, 'correct': 0, 'pending': len(selected), 'accuracy': None,
                'status': 'pending' if len(selected) == 50 else 'insufficient_sample',
                'repair_id': repair['id'] if repair else None,
                'verifier_version': repair['verifier_version'] if repair else None}
            db.execute('INSERT INTO uncertainty_audit_batches VALUES (?,?,?,?)',
                       (batch['id'], week, sampling_key, canonical(batch)))
            for assessment in selected:
                entry = {'id': uuid4().hex, 'batch_id': batch['id'], 'assessment_id': assessment['id'],
                    'trip_id': assessment['trip_id'], 'snapshot': assessment,
                    'human_correct': None, 'reviewer_id': None, 'reason': None, 'reviewed_at': None}
                db.execute('INSERT INTO uncertainty_audit_log VALUES (?,?,?,?,?)',
                    (entry['id'], batch['id'], assessment['id'], assessment['trip_id'], canonical(entry)))
            return self._batch(db, batch['id'])

    def _candidates(self, db, repair):
        # Audit what was asserted at that time, including expired/withdrawn
        # inventory. Sampling only today's surviving checkmarks hides errors.
        rows = db.execute('SELECT data FROM uncertainty_assessments ORDER BY rowid').fetchall()
        candidates = {}
        end = instant(self.store.clock())
        start = end - timedelta(days=7)
        for row in rows:
            snapshot = json.loads(row[0])
            assessed = instant(snapshot['assessed_at'])
            if not start <= assessed <= end or len(snapshot.get('checks', {})) != 6:
                continue
            if not all(snapshot['checks'].values()) or snapshot['tier'] not in ({'verified', 'conditional'} if repair else {'verified'}):
                continue
            if repair and (snapshot['verifier_version'] != repair['verifier_version']
                           or instant(snapshot['assessed_at']) < instant(repair['recorded_at'])):
                continue
            candidates[(snapshot['trip_id'], snapshot['claim_id'])] = snapshot
        return list(candidates.values())

    def _batch(self, db, batch_id):
        row = db.execute('SELECT data FROM uncertainty_audit_batches WHERE id=?', (batch_id,)).fetchone()
        if not row:
            raise UncertaintyError('NOT_FOUND', '审计批次不存在', 404)
        batch = json.loads(row[0])
        batch['items'] = [json.loads(r[0]) for r in db.execute(
            'SELECT data FROM uncertainty_audit_log WHERE batch_id=? ORDER BY rowid', (batch_id,)).fetchall()]
        return batch

    def get_batch(self, batch_id):
        """Operator-only aggregate. Never expose this across trip-token APIs."""
        with self.store._using() as db:
            return self._batch(db, batch_id)

    def annotate(self, batch_id, entry_id, *, correct, reason, actor):
        reviewer = human_identity(actor)
        if type(correct) is not bool:
            raise UncertaintyError('BAD_VERDICT', '人工判定必须为对或错')
        require_text(reason, 'reason')
        with self.store._using(write=True) as db:
            row = db.execute('SELECT data FROM uncertainty_audit_log WHERE id=? AND batch_id=?',
                             (entry_id, batch_id)).fetchone()
            if not row:
                raise UncertaintyError('NOT_FOUND', '审计条目不存在', 404)
            entry = json.loads(row[0])
            if entry['human_correct'] is not None:
                raise UncertaintyError('ALREADY_REVIEWED', '审计判定不可覆盖，请用新批次复审', 409)
            entry.update(human_correct=correct, reviewer_id=reviewer, reason=reason, reviewed_at=self.store.clock())
            db.execute('UPDATE uncertainty_audit_log SET data=? WHERE id=?', (canonical(entry), entry_id))
            return self._evaluate(db, batch_id)

    def _evaluate(self, db, batch_id):
        batch = self._batch(db, batch_id)
        items = batch.pop('items')
        reviewed = sum(item['human_correct'] is not None for item in items)
        correct = sum(item['human_correct'] is True for item in items)
        batch.update(reviewed=reviewed, correct=correct, pending=len(items) - reviewed,
                     accuracy=correct / reviewed if reviewed else None)
        control = self.store._audit_control(db)
        if len(items) < 50:
            batch['status'] = 'insufficient_sample'
        elif reviewed < 50:
            batch['status'] = 'pending'
        elif correct <= 47:
            batch['status'] = 'failed'
            revoked = set(control.get('revoked_verifier_versions', []))
            revoked.update(item['snapshot']['verifier_version'] for item in items)
            control.update(automatic_verified=False, status='audit_failed', failed_batch_id=batch_id,
                           repair=None, reason='human_accuracy_below_95_percent',
                           revoked_verifier_versions=sorted(revoked))
        else:
            batch['status'] = 'passed'
            repair = control.get('repair')
            restored = (repair and batch['repair_id'] == repair['id']
                        and batch_id != control['failed_batch_id'])
            if control['automatic_verified'] or restored:
                control.update(automatic_verified=True, status='audited', reason=None,
                               last_passed_batch_id=batch_id)
        db.execute('UPDATE uncertainty_audit_batches SET data=? WHERE id=?', (canonical(batch), batch_id))
        self._save_control(db, control)
        return batch | {'automatic_verified': control['automatic_verified']}

    def record_repair(self, *, repair_ref, verifier_version, actor):
        reviewer = human_identity(actor)
        require_text(repair_ref, 'repair_ref')
        require_text(verifier_version, 'verifier_version')
        with self.store._using(write=True) as db:
            control = self.store._audit_control(db)
            if control['automatic_verified']:
                raise UncertaintyError('NOT_SUSPENDED', '尚无待修复的审计停用', 409)
            if verifier_version in control.get('revoked_verifier_versions', []):
                raise UncertaintyError('NEW_VERIFIER_VERSION_REQUIRED', '修复须登记新的核验器版本', 409)
            repair = {'id': uuid4().hex, 'repair_ref': repair_ref, 'verifier_version': verifier_version,
                      'recorded_by': reviewer, 'recorded_at': self.store.clock(),
                      'failed_batch_id': control['failed_batch_id']}
            control.update(repair=repair, status='repair_pending_human_recheck')
            self._save_control(db, control)
            return repair

    def weekly_report(self):
        with self.store._using() as db:
            batches = [json.loads(row[0]) for row in db.execute(
                'SELECT data FROM uncertainty_audit_batches ORDER BY rowid').fetchall()]
            return {'control': self.store._audit_control(db), 'batches': batches,
                    'human_audits_completed': sum(b.get('status') in {'passed', 'failed'} for b in batches)}
