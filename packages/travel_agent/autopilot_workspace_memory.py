"""Candidate memory and authenticated material lifecycle, with no account identity fiction."""
import math
import copy
from uuid import uuid4
from .autopilot_contract import fail, text
from .autopilot_workspace_records import source_ref, visible
from .intake import check_source_budget


def timestamp(value):
    if type(value) not in (int,float) or not math.isfinite(value):
        fail('INVALID_ARGUMENT',400)
    return value


class MemoryMethods:
    def _expiry(self, value):
        if value is not None and timestamp(value)<=self.clock():
            fail('INVALID_ARGUMENT',400)
        return value

    def _propose_memory(self, db, run, trip, content, kind='inference', source_refs=None, expires_at=None):
        text(content)
        if kind not in {'inference','observation'}:
            fail('INVALID_ARGUMENT',400)
        row = self._new(trip,'memory',run,content=content,memory_kind=kind,status='candidate',
                        scope='trip',source_refs=self._dependencies(trip,source_refs or []),
                        expires_at=self._expiry(expires_at))
        self._put(db,row)
        return dict(row,candidate_id=row['id'])

    def record_correction(self, trip_id, token, content, source_refs=None, expires_at=None):
        text(content)
        with self.store.transaction() as db:
            trip = self.store._trip(db,trip_id,token)
            row = self._new(trip,'memory',content=content,memory_kind='explicit_correction',status='active',
                            scope='trip',source_refs=self._refs(trip,source_refs or []),
                            expires_at=self._expiry(expires_at))
            return self._put(db,row)

    def _retrieve_memory(self, db, run, trip, query='', scope='trip', as_of=None):
        if scope!='trip':
            fail('CROSS_TRIP_MEMORY_UNSUPPORTED',400)
        if not isinstance(query,str):
            fail('INVALID_ARGUMENT',400)
        as_of = self.clock() if as_of is None else timestamp(as_of)
        if as_of>self.clock():
            fail('FUTURE_AS_OF',400)
        rows = [row for row in self._rows(db,trip['id'],'memory')
                if self._context_eligible(trip, row, as_of, {'candidate', 'active'})
                and query.casefold() in row['content'].casefold()]
        return dict(items=rows,scope='trip',as_of=as_of,cross_trip_supported=False)

    def _context_eligible(self, trip, row, as_of, statuses):
        # Legacy inputs were created as trip-owned rows before scope was explicit.
        scope = row.get('scope', 'trip' if row.get('kind') == 'input' else None)
        if (row.get('trip_id') != trip['id'] or row.get('owner') != trip['id'] or
                scope != 'trip' or row.get('status') not in statuses):
            return False
        for key in ('created_at', 'answered_at'):
            value = row.get(key)
            if key == 'answered_at' and (row.get('kind') != 'input' or row.get('status') != 'answered'):
                continue
            if (type(value) not in (int, float) or not math.isfinite(value) or value > as_of):
                return False
        expires = row.get('expires_at')
        if expires is not None and (type(expires) not in (int, float) or
                                    not math.isfinite(expires) or expires <= as_of):
            return False
        return self._valid(trip, row, as_of)

    def context_user_state(self, db, trip):
        """Trusted model context projection; caller authenticates trip in this transaction."""
        as_of = self.clock()
        corrections = [r for r in self._rows(db, trip['id'], 'memory')
                       if r.get('memory_kind') == 'explicit_correction'
                       and self._context_eligible(trip, r, as_of, {'active'})]
        answers = [r for r in self._rows(db, trip['id'], 'input')
                   if self._context_eligible(trip, r, as_of, {'answered'})]
        return copy.deepcopy(dict(user_corrections=corrections, answered_inputs=answers))

    def context_goal(self, db, trip, goal):
        """Project denormalized correction text from current authoritative records only."""
        if goal.get('trip_id') != trip['id']:
            fail('GOAL_MISMATCH', 403)
        result = copy.deepcopy(goal)
        result.setdefault('constraints', {})['user_corrections'] = self.context_user_state(
            db, trip)['user_corrections']
        return result

    def ingest_material(self, trip_id, token, text, source_id=None):
        from .autopilot_contract import text as validate_text
        validate_text(text)
        with self.store.transaction() as db:
            trip = self.store._trip(db,trip_id,token)
            sources = trip.setdefault('material_sources',[])
            if source_id is not None:
                validate_text(source_id)
                source = next((s for s in sources if s['id']==source_id),None)
                if source is None:
                    fail('SOURCE_NOT_FOUND',404)
                version = source.get('workspace_version',1)+1
                self._invalidate(db,trip,source_id)
                source.update(content=text,status='read',revoked=False,workspace_version=version,observed_at=self.clock())
            else:
                source = dict(id='workspace-'+uuid4().hex,kind='user_text',label='User material',content=text,
                              status='read',workspace_version=1,observed_at=self.clock())
                sources.append(source)
            check_source_budget(sources)
            self.store.trip_store._save(db,trip)
            return dict(source_ref(source),version=source['workspace_version'],observed_at=source['observed_at'])

    def retract_material(self, trip_id, token, source_id):
        with self.store.transaction() as db:
            trip = self.store._trip(db,trip_id,token)
            source = next((s for s in trip.get('material_sources',[]) if s['id']==source_id),None)
            if source is None:
                fail('SOURCE_NOT_FOUND',404)
            self._invalidate(db,trip,source_id)
            source.update(revoked=True,status='withdrawn',revoked_at=self.clock())
            self.store.trip_store._save(db,trip)
            return dict(source_id=source_id,status='withdrawn')

    def _invalidate(self, db, trip, source_id):
        for kind in ('work','memory','job','input'):
            for row in self._rows(db,trip['id'],kind):
                if any(ref['source_id']==source_id for ref in row.get('source_refs',[])):
                    terminal_job = kind == 'job' and row['status'] in {'succeeded', 'failed', 'cancelled'}
                    row.update(invalidated_at=self.clock())
                    if not terminal_job:
                        row['status'] = 'invalidated'
                    self._put(db,row)
        # Existing plan provenance is not guaranteed complete: suppress its derived text
        # until a caller supplies a freshly built plan via the existing revision workflow.
        from .autopilot_contract import fingerprint
        trip['workspace_stale_plan_hash'] = fingerprint(trip.get('plan'))
