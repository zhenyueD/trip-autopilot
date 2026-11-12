"""Explicit local traveler profiles. Random IDs are local capability handles."""
from copy import deepcopy
import json
import secrets

from .single_session_api import APIError, TRIP_ID, _atomic_json, _now, _text, validate_intake

FIELDS = frozenset({'travelers', 'composition', 'pace', 'preferences', 'notes'})
INTAKE_FIELDS = FIELDS - {'notes'}


class ProfileStore:
    """Share SessionService's directory ownership and write lock; never infer data."""

    def __init__(self, directory, guard):
        self.path = directory / 'traveler-profiles.json'
        self.guard = guard
        if self.path.is_symlink():
            raise ValueError('profile store must not be a symlink')
        self.state = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {
            'profiles': {}, 'default_id': None}

    def _record(self, profile_id):
        if not isinstance(profile_id, str) or not TRIP_ID.fullmatch(profile_id):
            raise APIError(422, 'invalid_profile_id', 'profile_id must be a 32-character hex ID')
        if profile_id not in self.state['profiles']:
            raise APIError(404, 'profile_not_found', 'profile not found')
        return self.state['profiles'][profile_id]

    def _public(self, record):
        return {'profile': {**deepcopy(record), 'is_default': self.state['default_id'] == record['id']}}

    def _validate(self, payload, update=False):
        allowed = FIELDS | {'is_default'} | ({'clear_fields'} if update else set())
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise APIError(422, 'invalid_profile', 'provide supported explicit profile fields')
        if 'is_default' in payload and type(payload['is_default']) is not bool:
            raise APIError(422, 'invalid_profile', 'is_default must be boolean')
        clear = payload.get('clear_fields', [])
        if (not isinstance(clear, list) or any(not isinstance(k, str) or k not in FIELDS for k in clear)
                or set(clear) & set(payload)):
            raise APIError(422, 'invalid_profile', 'clear_fields must name fields not also assigned')
        values = {k: v for k, v in payload.items() if k in INTAKE_FIELDS}
        validated = validate_intake({'brief': 'profile validation', **values})
        values = {k: validated[k] for k in values}
        if 'notes' in payload:
            values['notes'] = _text(payload['notes'], 'notes', 30000)
        if any(v == '' or v == [] or (isinstance(v, list) and any(not s for s in v)) for v in values.values()):
            raise APIError(422, 'explicit_clear_required', 'use clear_fields to clear stored preferences')
        return values, clear

    def _commit(self, state):
        _atomic_json(self.path, state)
        self.state = state

    def create(self, payload):
        values, _ = self._validate(payload)
        with self.guard:
            state = deepcopy(self.state)
            profile_id = secrets.token_hex(16)
            record = {'id': profile_id, 'revision': 1, 'created_at': _now(), 'updated_at': _now(), **values}
            state['profiles'][profile_id] = record
            if payload.get('is_default'):
                state['default_id'] = profile_id
            self._commit(state)
            return self._public(record)

    def get(self, profile_id):
        with self.guard:
            return self._public(self._record(profile_id))

    def update(self, profile_id, payload):
        values, clear = self._validate(payload, update=True)
        with self.guard:
            self._record(profile_id)
            state = deepcopy(self.state)
            record = state['profiles'][profile_id]
            for key in clear:
                record.pop(key, None)
            record.update(values)
            record.update(revision=record['revision'] + 1, updated_at=_now())
            if payload.get('is_default'):
                state['default_id'] = profile_id
            elif payload.get('is_default') is False and state['default_id'] == profile_id:
                state['default_id'] = None
            self._commit(state)
            return self._public(record)

    def delete(self, profile_id):
        with self.guard:
            self._record(profile_id)
            state = deepcopy(self.state)
            del state['profiles'][profile_id]
            if state['default_id'] == profile_id:
                state['default_id'] = None
            self._commit(state)
            return {'deleted': profile_id}

    def apply(self, payload):
        """Resolve one immutable snapshot; explicit request keys always win."""
        if not isinstance(payload, dict):
            return validate_intake(payload), None
        intake = validate_intake({k: v for k, v in payload.items() if k != 'profile_id'})
        with self.guard:
            profile_id = payload.get('profile_id')
            if 'profile_id' not in payload:
                if 'composition' in payload and 'preferences' in payload:
                    return intake, None
                profile_id = self.state['default_id']
                if profile_id is None:
                    return intake, None
            record = self._record(profile_id)
            fields = {}
            for key in sorted(INTAKE_FIELDS):
                if key not in payload and key in record:
                    intake[key] = deepcopy(record[key])
                    fields[key] = {'source': 'profile', 'value': deepcopy(record[key])}
            return intake, {'profile_id': profile_id, 'profile_revision': record['revision'], 'fields': fields}
