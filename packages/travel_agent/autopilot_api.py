"""Small explicit route allowlist. User payloads cannot become server authority."""
import os
import re
import threading
from .autopilot_contract import fail
from .autopilot_service import AutopilotService, configured_limits
from .autopilot_intake import MATERIAL_FIELDS

PREFIX = '/api/agent/autopilot'
_instance = None
_instance_lock = threading.Lock()
FIELDS = {
    'create': {'brief','evidence_text','objective','success_criteria','auto_apply','background_check_allowance','allow_background_check','public_search_entities','client_token'},
    'goals': {'objective','success_criteria','auto_apply','background_check_allowance','allow_background_check','public_search_entities','expected_revision'},
    'events': {'request'}, 'materials': {'text','source_id'}, 'retract': set(),
    'answer': {'answer'}, 'corrections': {'content'},
    'apply': {'proposal_id','expected_revision'}, 'undo': {'revision_id','expected_revision'},
    'confirm': {'expected_goal_version'}, 'cancel': {'expected_goal_version'},
}
FIELDS['create'] |= MATERIAL_FIELDS
FIELDS['goals'] |= MATERIAL_FIELDS
FIELDS['events'] |= MATERIAL_FIELDS | {'expected_revision'}


def get_service():
    global _instance
    if os.getenv('TRAVEL_AUTOPILOT_ENABLED') != '1':
        fail('AUTOPILOT_DISABLED', 404)
    with _instance_lock:
        if _instance is None:
            # Lazy import avoids old API initialization while feature is disabled.
            from .api import service
            _instance = AutopilotService(service().store)
    return _instance


def start_worker():
    if os.getenv('TRAVEL_AUTOPILOT_ENABLED') == '1':
        get_service().start()


def body_for(action, body):
    if not isinstance(body, dict) or set(body) - FIELDS[action] - {'request_id'}:
        fail('INVALID_ARGUMENT', 400)
    request_id = body.get('request_id')
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id)>128:
        fail('REQUEST_ID_REQUIRED', 400)
    return body


def handle(method, path, body=None, token='', *, instance=None):
    suffix = path[len(PREFIX):] if path.startswith(PREFIX) else ''
    if method == 'GET' and suffix == '/capabilities':
        if instance is not None:
            return instance.capabilities(), 200
        if os.getenv('TRAVEL_AUTOPILOT_ENABLED') != '1':
            return dict(enabled=False, model_enabled=False, tools=[], external_transactions=False,
                        run_limits=configured_limits()), 200
        return get_service().capabilities(), 200
    service = instance or get_service()
    if not service.enabled:
        fail('AUTOPILOT_DISABLED', 404)
    if method == 'POST' and suffix == '/trips':
        return service.create(body_for('create', body)), 202
    trip = re.fullmatch(r'/trips/([a-f0-9]{32})/(state|goals|materials|corrections|apply|undo)', suffix)
    if trip:
        trip_id, action = trip.groups()
        # Authenticate before parsing input or resolving associated resources.
        service.trips.get(trip_id, token)
        if method == 'GET' and action == 'state':
            return service.state(trip_id, token), 200
        if method == 'POST' and action != 'state':
            return service.mutate_trip(trip_id, token, action, body_for(action, body)), 200 if action in {'apply','undo'} else 202
    nested = re.fullmatch(r'/trips/([a-f0-9]{32})/(materials|inputs)/([^/]+)/(retract|answer)', suffix)
    if nested and method == 'POST':
        trip_id, kind, object_id, action = nested.groups()
        service.trips.get(trip_id, token)
        if (kind,action) in {('materials','retract'),('inputs','answer')}:
            return service.mutate_trip(trip_id, token, action, body_for(action, body), object_id), 202
    goal = re.fullmatch(r'/goals/([a-f0-9]{32})/(events|confirm|cancel)', suffix)
    if goal and method == 'POST':
        goal_id, action = goal.groups()
        service.store.get_goal(goal_id, token)
        return service.mutate_goal(goal_id, token, action, body_for(action, body)), 202 if action == 'events' else 200
    fail('NOT_FOUND', 404)
