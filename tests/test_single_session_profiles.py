"""Offline profile contracts: real persistence/dispatch, no sockets or workers."""
from copy import deepcopy
from email.message import Message
from io import BytesIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from travel_agent.single_session_api import APIError, SessionHandler, SessionService, validate_intake


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'data'
        self.service = SessionService(self.root, lambda _: self.fail('agent must not run'))
        self.addCleanup(lambda: self.service.close())
        self.counter = 0

    def profile(self, **overrides):
        return self.service.profiles.create({
            'travelers': 3, 'composition': ['夫妻', '老人'], 'pace': '舒缓',
            'preferences': ['不自驾', '饮食清淡'], 'notes': '仅存档的私密备注',
            **overrides})['profile']

    def trip(self, **payload):
        self.counter += 1
        with patch.object(self.service.executor, 'submit'):
            return self.service.create({'brief': f'离线目的地 {self.counter}', **payload})

    def intake(self, trip):
        return self.service.records[trip['trip']['id']]['intake']

    def request(self, method, path, payload=None, headers=None, raw=None):
        handler = object.__new__(SessionHandler)
        handler.server = SimpleNamespace(service=self.service, server_address=('127.0.0.1', 7392), static_root=None)
        body = raw if raw is not None else json.dumps(payload).encode()
        handler.headers = Message()
        for key, value in {'Host': '127.0.0.1:7392', 'Content-Type': 'application/json',
                           'Content-Length': str(len(body)), **(headers or {})}.items():
            handler.headers[key] = value
        handler.connection = Mock()
        handler.rfile = BytesIO(body)
        handler.command, handler.path = method, path
        replies = []
        handler._reply = lambda status, value: replies.append((status, value))
        with patch.object(self.service.executor, 'submit'):
            handler._handle()
        self.assertEqual(len(replies), 1)
        return replies[0]

    def test_crud_preserves_omitted_fields_and_explicit_clear(self):
        original = self.profile()
        profile_id = original['id']
        self.assertRegex(profile_id, r'^[0-9a-f]{32}$')
        self.assertFalse(original['is_default'])
        self.assertEqual(self.service.profiles.get(profile_id)['profile'], original)
        updated = self.service.profiles.update(profile_id, {'pace': '紧凑'})['profile']
        self.assertEqual(updated['revision'], 2)
        self.assertEqual(updated['created_at'], original['created_at'])
        for field in ('travelers', 'composition', 'preferences', 'notes'):
            self.assertEqual(updated[field], original[field])
        cleared = self.service.profiles.update(profile_id, {'clear_fields': ['notes', 'pace']})['profile']
        self.assertNotIn('notes', cleared)
        self.assertNotIn('pace', cleared)
        self.assertEqual(cleared['preferences'], original['preferences'])

    def test_explicit_profile_applies_traceable_snapshot_without_notes(self):
        profile = self.profile()
        trip = self.trip(profile_id=profile['id'])
        fields = {key: {'source': 'profile', 'value': profile[key]}
                  for key in ('travelers', 'composition', 'pace', 'preferences')}
        self.assertEqual(trip['trip']['profile_application'], {
            'profile_id': profile['id'], 'profile_revision': 1, 'fields': fields})
        for field, detail in fields.items():
            self.assertEqual(self.intake(trip)[field], detail['value'])
        self.assertNotIn('notes', self.intake(trip))
        self.assertNotIn(profile['notes'], json.dumps(trip, ensure_ascii=False))
        self.service.profiles.update(profile['id'], {'preferences': '怕冷'})
        saved = self.service.get(trip['trip']['id'], trip['token'])['trip']
        self.assertEqual(saved['profile_application']['profile_revision'], 1)
        self.assertEqual(saved['profile_application']['fields'], fields)

    def test_default_applies_when_either_composition_or_preferences_missing(self):
        profile = self.profile(is_default=True)
        for payload in ({}, {'composition': '朋友'}, {'preferences': '爱步行'}):
            with self.subTest(payload=payload):
                trip = self.trip(**payload)
                self.assertEqual(trip['trip']['profile_application']['profile_id'], profile['id'])
                for key, value in payload.items():
                    self.assertEqual(self.intake(trip)[key], value)
                    self.assertNotIn(key, trip['trip']['profile_application']['fields'])
        trip = self.trip(composition='朋友', preferences='爱步行')
        self.assertNotIn('profile_application', trip['trip'])
        self.assertNotIn('travelers', self.intake(trip))

    def test_explicit_values_including_empty_win(self):
        profile = self.profile(is_default=True)
        payload = {'travelers': 1, 'composition': [], 'pace': '', 'preferences': ''}
        trip = self.trip(profile_id=profile['id'], **payload)
        for key, value in payload.items():
            self.assertEqual(self.intake(trip)[key], value)
        self.assertEqual(trip['trip']['profile_application']['fields'], {})
        trip = self.trip(composition=[], preferences=[])
        self.assertNotIn('profile_application', trip['trip'])

    def test_without_profile_and_without_default_preserves_legacy_intake(self):
        for existing_profile in (False, True):
            if existing_profile:
                self.profile()
            trip = self.trip()
            intake = deepcopy(self.intake(trip))
            intake.pop('case_id')
            self.assertEqual(intake, validate_intake({'brief': trip['trip']['brief']}))
            self.assertNotIn('profile_application', trip['trip'])

    def test_delete_default_does_not_promote_other_profile(self):
        other = self.profile()
        default = self.profile(is_default=True)
        previous = self.trip()
        self.assertEqual(self.service.profiles.delete(default['id']), {'deleted': default['id']})
        self.assertNotIn('profile_application', self.trip()['trip'])
        self.assertFalse(self.service.profiles.get(other['id'])['profile']['is_default'])
        self.assertEqual(previous['trip']['profile_application']['profile_id'], default['id'])
        with self.assertRaises(APIError) as error:
            self.trip(profile_id=default['id'])
        self.assertEqual(error.exception.status, 404)

    def test_default_switch_unset_and_restart_persist(self):
        first = self.profile(is_default=True)
        second = self.profile()
        self.service.profiles.update(second['id'], {'is_default': True, 'preferences': '怕冷'})
        self.assertFalse(self.service.profiles.get(first['id'])['profile']['is_default'])
        self.service.close()
        self.service = SessionService(self.root, lambda _: self.fail('restore must not call agent'))
        self.assertEqual(self.trip()['trip']['profile_application']['profile_id'], second['id'])
        self.assertEqual(self.service.profiles.get(second['id'])['profile']['preferences'], '怕冷')
        self.service.profiles.update(second['id'], {'is_default': False})
        self.assertNotIn('profile_application', self.trip()['trip'])
        self.assertEqual((self.root / 'traveler-profiles.json').stat().st_mode & 0o777, 0o600)

    def test_revision_text_never_updates_profile(self):
        profile = self.profile(is_default=True)
        trip = self.trip()
        path = self.root / 'traveler-profiles.json'
        before = path.read_bytes()
        record = self.service.records[trip['trip']['id']]
        record.update(status='completed', document={'version': 1, 'hash': 'offline'})
        record['jobs'][-1]['status'] = 'completed'
        with patch.object(self.service.executor, 'submit') as submit:
            self.service.revise(record['id'], trip['token'], {'feedback': '爸妈怕冷，改去暖和的地方', 'expected_revision': 1})
        submit.assert_called_once()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.service.profiles.get(profile['id'])['profile'], profile)

    def test_invalid_input_cannot_clear_or_mutate_store(self):
        profile = self.profile()
        path = self.root / 'traveler-profiles.json'
        before = path.read_bytes()
        invalid = [None, [], {'travelers': True}, {'travelers': 0}, {'travelers': 101},
                   {'composition': {}}, {'preferences': [3]}, {'preferences': [' ']},
                   {'pace': None}, {'pace': ''}, {'preferences': []}, {'notes': ''},
                   {'notes': {}}, {'is_default': 1}, {'destination': '不应保存'},
                   {'clear_fields': 'notes'}, {'clear_fields': ['id']},
                   {'clear_fields': ['pace'], 'pace': '慢'}]
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(APIError) as error:
                    self.service.profiles.update(profile['id'], payload)
                self.assertEqual(error.exception.status, 422)
                self.assertEqual(path.read_bytes(), before)
        for bad_id in ('../outside', '', None, 3, 'A' * 32):
            with self.subTest(profile_id=bad_id):
                with self.assertRaises(APIError) as error:
                    self.trip(profile_id=bad_id)
                self.assertEqual(error.exception.status, 422)
        self.assertEqual(self.service.records, {})

    def test_public_values_do_not_alias_store(self):
        profile = self.profile()
        profile['preferences'].append('意外修改')
        fresh = self.service.profiles.get(profile['id'])['profile']
        self.assertNotIn('意外修改', fresh['preferences'])
        trip = self.trip(profile_id=profile['id'])
        trip['trip']['profile_application']['fields']['preferences']['value'].append('回执修改')
        self.assertNotIn('回执修改', self.intake(trip)['preferences'])

    def test_failed_atomic_write_preserves_previous_profile(self):
        profile = self.profile(is_default=True)
        path = self.root / 'traveler-profiles.json'
        before = path.read_bytes()
        with patch('travel_agent.single_session_profiles._atomic_json', side_effect=OSError('offline write failure')):
            with self.assertRaises(OSError):
                self.service.profiles.update(profile['id'], {'preferences': '不应保存'})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.service.profiles.get(profile['id'])['profile'], profile)

    def test_http_crud_trip_dispatch_and_no_list_endpoint(self):
        status, result = self.request('POST', '/api/agent/profiles', {'preferences': '素食', 'is_default': True})
        self.assertEqual(status, 201)
        profile_id = result['profile']['id']
        route = '/api/agent/profiles/' + profile_id
        self.assertEqual(self.request('GET', route), (200, result))
        status, result = self.request('PATCH', route, {'pace': '慢'})
        self.assertEqual(status, 200)
        self.assertEqual(result['profile']['revision'], 2)
        status, trip = self.request('POST', '/api/agent/trips', {'brief': '离线', 'profile_id': profile_id})
        self.assertEqual(status, 202)
        self.assertEqual(trip['trip']['profile_application']['fields']['preferences']['value'], '素食')
        self.assertEqual(self.request('GET', '/api/agent/profiles')[0], 404)
        self.assertEqual(self.request('DELETE', route), (200, {'deleted': profile_id}))
        self.assertEqual(self.request('GET', route)[0], 404)
        self.assertEqual(self.request('POST', '/api/agent/trips', {'brief': '另一个离线', 'profile_id': profile_id})[0], 404)

    def test_http_origin_path_and_json_guards(self):
        profile = self.profile()
        route = '/api/agent/profiles/' + profile['id']
        for method in ('GET', 'PATCH', 'DELETE'):
            for headers in ({'Origin': 'https://evil.example'}, {'Host': 'evil.example'}):
                self.assertEqual(self.request(method, route, {'pace': '快'}, headers=headers)[0], 403)
        self.assertEqual(self.request('POST', '/api/agent/profiles', {}, headers={'Origin': 'null'})[0], 403)
        for suffix in ('../traveler-profiles.json', '%2e%2e%2fsecret', '0' * 32, 'bad'):
            self.assertEqual(self.request('GET', '/api/agent/profiles/' + suffix)[0], 404)
        self.assertEqual(self.request('PATCH', route, raw=b'{')[0], 400)
        self.assertEqual(self.request('PATCH', route, {}, headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.service.profiles.get(profile['id'])['profile'], profile)


if __name__ == '__main__':
    unittest.main()
