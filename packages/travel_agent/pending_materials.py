"""Round-local material selection, independent of the plan's undo snapshot."""
from .detail_reviews import source_withdrawn


def active_materials(trip):
    pending = {row['source_id'] for row in trip.get('pending_materials') or []}
    return [row for row in trip.get('material_sources') or []
            if row['id'] not in pending and not source_withdrawn(row)]


def assigned_pending_ids(pending, feedback):
    """Mentioning a receipt selects it for assessment, never grants a scope."""
    return {row['source_id'] for row in pending
            if any(value and value in feedback for value in (row.get('label'), row.get('source_id')))}


def revision_sources(trip, feedback=''):
    sources = trip.get('material_sources') or []
    if 'revision_material_ids' in trip:
        ids = set(trip['revision_material_ids'])
    else:
        # Compatibility for frozen pre-marker records, not a persistent cursor.
        previous = ((trip.get('revision_base') or {}).get('material_state') or {}).get('material_sources') or []
        ids = {row['id'] for row in sources} - {row['id'] for row in previous}
    ids.update(assigned_pending_ids(trip.get('pending_materials') or [], feedback))
    return [row for row in sources if row['id'] in ids and not source_withdrawn(row)]


def pending_after_assessment(existing, assessed, scope):
    examined = {row['id'] for row in assessed}
    pending = {row['source_id']: row for row in existing if row['source_id'] not in examined}
    pending.update({row['source_id']: row for row in scope.get('pending_materials', [])})
    return list(pending.values())
