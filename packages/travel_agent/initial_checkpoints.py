"""Initial stage snapshots using the existing private revision checkpoint slot."""
import copy

from .initial_contract import VERSION
from .revision_plan import plan_sha256
from .traveler_profile import current_profile


def request_hash(trip, autonomous):
    return plan_sha256({'version': VERSION, 'autonomous': autonomous,
        'brief': trip.get('brief'), 'feedback': trip.get('feedback', []),
        'materials': trip.get('material_sources', []),
        'locks': trip.get('locked_item_ids', []), 'profile': current_profile(trip)})


def restore_stage(trip, autonomous):
    checkpoint = trip.get('revision_checkpoint') or {}
    if trip.get('plan') or checkpoint.get('kind') != 'initial_generation':
        return None
    stage = checkpoint.get('stage_inputs')
    if (checkpoint.get('version') != VERSION or not isinstance(stage, dict)
            or checkpoint.get('request_sha256') != request_hash(trip, autonomous)
            or checkpoint.get('stage_sha256') != plan_sha256(stage)):
        return None
    return copy.deepcopy(stage)


def save_stage(service, trip, payload, research, usage):
    """Freeze expensive, validated inputs before any generated day can fail."""
    stage = copy.deepcopy({'payload': payload, 'research': research})
    checkpoint = {'kind': 'initial_generation', 'version': VERSION,
        'request_sha256': request_hash(trip, service.autonomous),
        'stage_inputs': stage, 'stage_sha256': plan_sha256(stage),
        'status': 'stage_ready', 'usage': copy.deepcopy(usage)}
    service.store.update_job(trip['id'], trip['revision'], revision_checkpoint=checkpoint)
    trip['revision_checkpoint'] = checkpoint
    return checkpoint
