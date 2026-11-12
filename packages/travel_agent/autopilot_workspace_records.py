"""Workspace records and dependency validation; all writes use caller transaction."""
import copy
import hashlib
import json
import math
from datetime import datetime
from uuid import uuid4
from .autopilot_contract import canonical, fail, fingerprint, text
from .detail_reviews import source_withdrawn


def visible(source):
    return not (source.get('revoked') or source.get('status') in {'withdrawn','revoked'} or source_withdrawn(source))


def source_ref(source):
    return dict(source_id=source['id'], fingerprint=hashlib.sha256(source.get('content','').encode()).hexdigest())


def source_freshness(source, now):
    """TTL applicability, separate from access and semantic fact verification."""
    until = source.get('valid_until')
    state = 'unbounded'
    if until is not None:
        try:
            timestamp = datetime.fromisoformat(until.replace('Z', '+00:00'))
            if timestamp.tzinfo is None:
                raise ValueError('timezone required')
            state = 'expired' if timestamp.timestamp() <= now else 'current'
        except (ValueError, TypeError, AttributeError, OverflowError):
            state = 'invalid'
    return dict(valid_until=until, freshness=state,
                historical_only=state in {'expired', 'invalid'})


def material_state(trip, now):
    """Stable between applicability transitions; never hash the ticking clock."""
    materials = trip.get('material_sources', [])
    timed = [(key, source.get('id'), source_freshness(source, now))
             for key in ('material_sources', 'source_evidence')
             for source in trip.get(key) or [] if source.get('valid_until') is not None]
    return fingerprint(dict(materials=materials, source_validity=timed)) if timed else fingerprint(materials)


class RecordMethods:
    def reusable_detail_ledger(self, db, trip):
        """Reuse a successful source-bound receipt, never an old suitability verdict."""
        state = material_state(trip, self.clock())
        rows = db.execute('''SELECT o.data FROM autopilot_operations o
            JOIN autopilot_runs r ON r.id=o.run_id
            WHERE json_extract(r.data, '$.trip_id')=?
              AND json_extract(o.data, '$.name')='delegate'
              AND json_extract(o.data, '$.status')='succeeded'
            ORDER BY o.rowid DESC LIMIT 20''', (trip['id'],))
        available = {s['id'] for s in trip.get('material_sources', []) if self._visible(s)
                     and not source_freshness(s, self.clock())['historical_only']}
        for saved in rows:
            operation = json.loads(saved[0])
            result = operation.get('result') or {}
            structured = (result.get('material') or {}).get('structured') or {}
            ledger = structured.get('detail_ledger') or {}
            materials = ledger.get('materials') or []
            if (result.get('material_state') != state or structured.get('status') not in ('available', 'partial')
                    or not materials or any(row.get('material_id') not in available for row in materials)):
                continue
            ledger = copy.deepcopy(ledger)
            ledger['reused_from_operation'] = operation['id']
            ledger['current_applicability'] = 'not_assessed'
            for material in ledger['materials']:
                for detail in material.get('details', []) + material.get('guide_steps', []):
                    detail['applicability'] = dict(status='unknown', target=None,
                        assessment='current_suitability_not_assessed',
                        source_assessment=detail.get('applicability'),
                        reason='Reuse source details; evaluate this request separately')
            from .material_details import corroborate_material_details
            ledger['corroborations'] = corroborate_material_details(ledger, ledger.get('corroborations', []))
            return ledger
        return None

    def _visible(self, source, as_of=None):
        if not visible(source):
            return False
        observed = source.get('observed_at')
        if observed is None:
            return True
        if isinstance(observed, str):
            try:
                observed = datetime.fromisoformat(observed.replace('Z', '+00:00')).timestamp()
            except ValueError:
                return False
        return type(observed) in (int, float) and math.isfinite(observed) and observed <= (self.clock() if as_of is None else min(as_of, self.clock()))

    def _rows(self, db, trip_id, kind):
        return [json.loads(r[0]) for r in db.execute(
            'SELECT data FROM autopilot_workspace WHERE trip_id=? AND kind=? ORDER BY rowid', (trip_id,kind))]

    def _put(self, db, row):
        db.execute('INSERT OR REPLACE INTO autopilot_workspace(id,trip_id,kind,data) VALUES (?,?,?,?)',
                   (row['id'],row['trip_id'],row['kind'],canonical(row)))
        return row

    def _get(self, db, trip_id, kind, object_id):
        row = db.execute('SELECT data FROM autopilot_workspace WHERE id=? AND trip_id=? AND kind=?',
                         (object_id,trip_id,kind)).fetchone()
        if not row:
            fail('ARTIFACT_NOT_FOUND',404)
        return json.loads(row[0])

    def _new(self, trip, kind, run=None, **fields):
        return dict(id=uuid4().hex,trip_id=trip['id'],owner=trip['id'],kind=kind,
                    goal_id=run['goal_id'] if run else None,run_id=run['id'] if run else None,
                    created_at=self.clock(),updated_at=self.clock(),**fields)

    def _refs(self, trip, refs, as_of=None):
        if not isinstance(refs,list) or len(refs)>100:
            fail('INVALID_ARGUMENT',400)
        now = self.clock() if as_of is None else min(as_of, self.clock())
        available = {s['id']: source_ref(s) for s in trip.get('material_sources',[])
                     if self._visible(s, as_of) and not source_freshness(s, now)['historical_only']}
        for ref in refs:
            if not isinstance(ref,dict) or set(ref) != {'source_id','fingerprint'} or available.get(ref.get('source_id')) != ref:
                fail('SOURCE_UNAVAILABLE',409)
        return refs

    def _dependencies(self, trip, refs):
        # Conservatively bind unspecified provenance to all currently available material.
        return self._refs(trip, refs) if refs else [source_ref(s) for s in trip.get('material_sources',[]) if self._visible(s)]

    def _valid(self, trip, row, as_of=None):
        if (row.get('status') in {'invalidated','revoked','cancelled'} or
                row.get('revoked') or row.get('revoked_at') is not None):
            return False
        try:
            self._refs(trip,row.get('source_refs',[]), as_of)
        except Exception as error:
            from .store import StateError
            if not isinstance(error, StateError):
                raise
            return False
        return True

    def _artifacts(self, db, trip, refs, work_id=None):
        if not isinstance(refs,list):
            fail('INVALID_ARGUMENT',400)
        dependencies = []
        for ref in refs:
            if not isinstance(ref,dict) or ref.get('kind') not in {'source','input','work','revision'}:
                fail('INVALID_ARGUMENT',400)
            text(ref.get('id'))
            if ref['kind']=='source':
                dependencies.extend(self._refs(trip,[dict(source_id=ref['id'],fingerprint=ref.get('fingerprint'))]))
            elif ref['kind']=='revision':
                # Revisions must be applied and owned by this trip; proposals are not artifacts.
                table = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='autopilot_revisions'").fetchone()
                row = db.execute('SELECT data FROM autopilot_revisions WHERE id=?',(ref['id'],)).fetchone() if table else None
                revision = json.loads(row[0]) if row else {}
                if revision.get('trip_id') != trip['id'] or revision.get('kind') != 'apply' or revision.get('committed_at') is None:
                    fail('ARTIFACT_NOT_FOUND',404)
                undone = db.execute('SELECT data FROM autopilot_revisions WHERE trip_id=?', (trip['id'],))
                if any(json.loads(r[0]).get('undo_of') == ref['id'] for r in undone):
                    fail('ARTIFACT_NOT_READY')
                dependencies.extend(self._dependencies(trip, []))
            else:
                if ref['id']==work_id:
                    fail('INVALID_ARGUMENT',400)
                row = self._get(db,trip['id'],ref['kind'],ref['id'])
                required = 'answered' if ref['kind']=='input' else 'completed'
                if row['status'] != required or not self._valid(trip,row):
                    fail('ARTIFACT_NOT_READY')
                dependencies.extend(row.get('source_refs',[]))
        return dependencies

    def _update_work(self, db, run, trip, title, status='pending', work_id=None,
                     artifact_refs=None, source_refs=None):
        text(title)
        if status not in {'pending','in_progress','waiting','completed','cancelled'}:
            fail('INVALID_ARGUMENT',400)
        artifacts = artifact_refs or []
        if status=='completed' and not artifacts:
            fail('ARTIFACT_REQUIRED')
        refs = self._dependencies(trip,source_refs or []) + self._artifacts(db,trip,artifacts,work_id)
        row = self._get(db,trip['id'],'work',work_id) if work_id else self._new(trip,'work',run)
        if row['goal_id'] != run['goal_id']:
            fail('GOAL_MISMATCH',403)
        row.update(title=title,status=status,artifact_refs=artifacts,source_refs=refs,updated_at=self.clock())
        self._put(db,row)
        return dict(row,work_id=row['id'])

    def _request_input(self, db, run, trip, question, key, reason, options=None):
        for value in (question,key,reason):
            text(value)
        if options is not None and (not isinstance(options, list) or not 2 <= len(options) <= 3
                or any(not isinstance(option, str) or not option.strip() or len(option) > 200
                       for option in options)):
            fail('INVALID_ARGUMENT',400)
        choices = [option.strip() for option in options] if options is not None else []
        if len({option.casefold() for option in choices}) != len(choices):
            fail('INVALID_ARGUMENT',400)
        for row in self._rows(db,trip['id'],'input'):
            if row.get('key')==key and row['goal_id']==run['goal_id'] and self._valid(trip,row):
                if row['question']!=question or row.get('options',[])!=choices:
                    fail('IDEMPOTENCY_CONFLICT')
                return dict(row,pending_input_id=row['id'])
        row = self._new(trip,'input',run,question=question,key=key,reason=reason,status='pending',
                        options=choices,answer=None,scope='trip',source_refs=self._dependencies(trip,[]))
        self._put(db,row)
        return dict(row,pending_input_id=row['id'])

    def answer_input(self, trip_id, token, input_id, answer):
        text(answer)
        with self.store.transaction() as db:
            trip = self.store._trip(db,trip_id,token)
            row = self._get(db,trip_id,'input',input_id)
            if not self._valid(trip,row):
                fail('INPUT_INVALIDATED')
            if row['status']=='answered' and row['answer']!=answer:
                fail('INPUT_ALREADY_ANSWERED')
            row.update(status='answered',answer=answer,answered_at=self.clock())
            self._put(db,row)
            return dict(row,pending_input_id=row['id'])

    def snapshot(self, trip_id, token):
        with self.store.transaction() as db:
            trip = self.store._trip(db,trip_id,token)
            result = {}
            for key,kind in [('work','work'),('inputs','input'),('memory','memory'),('jobs','job')]:
                if kind == 'job':
                    result[key] = [self._job_snapshot(db, trip, row)
                                   for row in self._rows(db, trip_id, kind)]
                    continue
                result[key] = [self._public(row) if self._valid(trip,row) else
                               dict(id=row['id'],status='invalidated',kind=kind)
                               for row in self._rows(db,trip_id,kind)]
            return result

    def _job_snapshot(self, db, trip, row):
        terminal = row['status'] in {'succeeded', 'failed', 'cancelled'}
        active = not terminal and self._job_valid(db, row)
        if terminal:
            # Retain historical outcome, but hide payload if its source is no longer valid.
            result = self._public(row) if self._valid(trip, row) else dict(
                id=row['id'], status=row['status'], kind='job')
            return dict(result, active=False)
        if not active:
            return dict(id=row['id'], status='invalidated', kind='job', active=False)
        return dict(self._public(row), active=True)

    def _public(self, row):
        return {k:v for k,v in row.items() if k not in {'token_hash','claim_secret'}}
