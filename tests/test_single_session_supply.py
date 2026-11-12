"""Offline integration through the real tool dispatcher and document store."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from packages.travel_agent.single_session_agent import SingleSessionAgent
from packages.travel_agent.supply_cache import SupplyCache
from tests.test_single_session_agent import call, URL, MODULE


def handbook(prose):
    data = dict(version=1, days=[dict(id='day-1', title='Offline day',
                date='2026-12-01', summary='Synthetic rest day',
                timeline=[dict(kind='activity', action='Rest')])])
    return '# Offline\n' + prose + '\n```destination-detail-v1\n' + json.dumps(data) + '\n```'


class SupplyWiringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.when = datetime.now(timezone.utc).isoformat()
        self.capture = dict(status='read', url=URL, text='Open daily.', fetched_at=self.when)
        self.reader = patch(MODULE + '.capture_jina_page', return_value=self.capture).start()
        self.addCleanup(patch.stopall)
        self.agent = self.new('first')

    def new(self, name, **intake):
        agent = SingleSessionAgent(self.root / name)
        agent.session['intake'] = dict(destination='Synthetic City', start_date='2026-12-01', **intake)
        return agent

    def execute(self, agent, i, name, **args):
        return agent._execute_tool(call(i, name, **args))

    def seed(self):
        read = self.execute(self.agent, 'r', 'read_page', url=URL)
        evidence = self.execute(self.agent, 'e', 'record_evidence', source_id=read['source_id'],
                                quote='Open daily.', claim='Hours', kind='hours')
        self.assertEqual(SupplyCache(self.agent.supply.path).read_destination('Synthetic City'), [])
        self.execute(self.agent, 's', 'save_handbook', markdown=handbook('Hours {{evidence:E1}}'))
        self.reader.reset_mock()
        return evidence

    def test_save_then_fresh_new_session_zero_network_and_ledger(self):
        original = self.seed()
        agent = self.new('second')
        read = self.execute(agent, 'r', 'read_page', url=URL)
        self.reader.assert_not_called()
        self.assertTrue(read['cached'])
        self.assertEqual(read['content'], 'Open daily.')
        event = next(e for e in agent.session['supply_cache_events'] if e['action'] == 'lookup')
        self.assertTrue(event['hit'])
        self.assertTrue(event['usable'])
        self.assertEqual(event['freshness'], 'fresh')
        self.assertEqual(event['verified_at'], original['verified_at'])
        record = self.execute(agent, 'e', 'record_evidence', source_id=read['source_id'],
                              quote='Open daily.', claim='Hours', kind='hours')
        self.assertEqual(record['verified_at'], original['verified_at'])
        self.execute(agent, 's', 'save_handbook', markdown=handbook('Hours {{evidence:E1}}'))
        self.assertEqual(agent.budget.snapshot()['used'], 0)
        self.assertEqual(agent.session['model_requests'], 0)
        self.assertEqual(len(SupplyCache(agent.supply.path).read_destination('Synthetic City')), 1)

    def test_cached_shorter_quote_keeps_original_time(self):
        original = self.seed()
        agent = self.new('shortened')
        read = self.execute(agent, 'r', 'read_page', url=URL)
        record = self.execute(agent, 'e', 'record_evidence', source_id=read['source_id'],
                              quote='daily', claim='Daily', kind='other')
        self.assertEqual(record['verified_at'], original['verified_at'])
        self.assertEqual(record['verification_status'], '单源未交叉确认')

    def test_real_initial_user_json_ignores_only_session_identity(self):
        self.agent.session['intake']['case_id'] = 'first-id'
        self.agent.session['messages'] = [dict(role='user', content=json.dumps(self.agent.session['intake']))]
        self.seed()
        other = self.new('real-json', case_id='second-id')
        other.session['messages'] = [dict(role='user', content=json.dumps(other.session['intake']))]
        self.execute(other, 'r', 'read_page', url=URL)
        self.reader.assert_not_called()

    def test_revision_cannot_relabel_live_evidence(self):
        self.seed()
        self.agent.session['intake']['destination'] = 'Different City'
        self.agent.session['messages'].append(dict(role='user', content='Change destination'))
        self.execute(self.agent, 's2', 'save_handbook', markdown=handbook('# Changed'))
        self.assertEqual(SupplyCache(self.agent.supply.path).read_destination('Different City'), [])

    def test_expired_supply_receipt_cannot_bypass_live_fallback(self):
        self.seed()
        agent = self.new('second')
        self.execute(agent, 'r1', 'read_page', url=URL)
        with sqlite3.connect(agent.supply.path) as db:
            key, body = db.execute('SELECT cache_key,body FROM atoms').fetchone()
            atom = json.loads(body)
            atom['verified_at'] = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
            atom['sources'][0]['retrieved_at'] = atom['verified_at']
            db.execute('UPDATE atoms SET body=? WHERE cache_key=?', (json.dumps(atom), key))
        self.reader.reset_mock()
        result = self.execute(agent, 'r2', 'read_page', url=URL)
        self.reader.assert_called_once_with(URL)
        self.assertFalse(result['cached'])

    def test_nested_api_session_directories_share_cache(self):
        from packages.travel_agent.single_session_api import _agent
        first = _agent(self.root / 'trip1' / 'session')
        second = _agent(self.root / 'trip2' / 'session')
        self.assertEqual(first.supply.path, second.supply.path)
        self.assertEqual(first.supply.path, self.root / 'supply-cache.sqlite3')

    def test_full_offline_turns_across_api_session_layout(self):
        from packages.travel_agent.single_session_api import _agent
        for name in ('trip1', 'trip2'):
            agent = _agent(self.root / name / 'session')
            intake = dict(destination='Synthetic City', start_date='2026-12-01', case_id=name)
            # First-turn generation now has bounded stages. Keep the original
            # two-session source-cache assertions, using the new model protocol.
            async def model():
                messages = agent.payload()['messages']
                data = json.loads(next(m['content'] for m in messages if m['role']=='user'))
                if 'case_id' in data:
                    value = dict(destination='Synthetic City', departure_date='2026-12-01',
                                 season_scope='fixture', categories=['开放'])
                elif 'category' in data:
                    if messages[-1]['role']=='user':
                        return dict(role='assistant',content=None,tool_calls=[
                            call('r','read_page',url=URL)]),'tool_calls'
                    value = {'candidates':[{'source_id':'S1','atom':dict(
                        destination='Synthetic City',claim='Hours',category='开放',s_level='S1',
                        source_name='fixture',source_url=URL,verbatim_quote='Open daily.',
                        checked_date='2026-11-06',expires_on='2099-01-01',season_scope='fixture',
                        evidence_kind='fixture',notes=[])}]}
                elif 'candidates' in data:
                    value = {'accepted':data['candidates'],'rejected':[]}
                elif 'composition' in data:
                    value = {'passed':True,'findings':[]}
                else:
                    ids = list(data['bindings'])
                    value = dict(markdown=handbook('Hours '+data['bindings'][ids[0]]['evidence']['marker']),
                                 fact_atom_ids=ids,estimates=[],self_check={'passed':True},prices=[])
                return dict(role='assistant',content=None,tool_calls=[
                    call('finish-'+str(len(messages)), 'finish_stage', result_json=json.dumps(value))]),'tool_calls'
            self.reader.reset_mock()
            with patch.dict(os.environ, {'TRAVEL_ORCH': '1'}), patch.object(agent, '_model', side_effect=model), patch(
                    'packages.travel_agent.single_session_orch_backend.render_pdf',
                    return_value={'status':'ready','scope':'unit-test-stub'}):
                result = asyncio.run(agent.run_turn(json.dumps(intake), intake=intake))
            self.assertTrue(result['document_saved'])
            self.assertEqual(agent.session['turns'][-1]['status'], 'completed')
            self.assertEqual(self.reader.call_count, 1 if name == 'trip1' else 0,
                             agent.session.get('supply_cache_events'))
            self.assertEqual(agent.budget.snapshot()['used'], 0)

    def test_expired_hit_falls_back_to_live(self):
        self.seed()
        path = self.agent.supply.path
        with sqlite3.connect(path) as db:
            key, body = db.execute('SELECT cache_key,body FROM atoms').fetchone()
            atom = json.loads(body)
            atom['verified_at'] = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
            atom['sources'][0]['retrieved_at'] = atom['verified_at']
            db.execute('UPDATE atoms SET body=? WHERE cache_key=?', (json.dumps(atom), key))
        agent = self.new('second')
        read = self.execute(agent, 'r', 'read_page', url=URL)
        self.reader.assert_called_once_with(URL)
        self.assertFalse(read['cached'])
        self.assertTrue(any(e.get('freshness') == 'expired' for e in agent.session['supply_cache_events']))

    def test_miss_falls_back_to_live(self):
        read = self.execute(self.agent, 'r', 'read_page', url=URL)
        self.reader.assert_called_once_with(URL)
        self.assertFalse(read['cached'])

    def test_corrupt_and_unreadable_cache_fall_back(self):
        for failure in ('corrupt', 'directory'):
            with self.subTest(failure=failure):
                agent = self.new(failure)
                agent.supply.path = self.root / failure / 'bad.db'
                if failure == 'corrupt':
                    agent.supply.path.write_text('not sqlite')
                else:
                    agent.supply.path.mkdir()
                self.reader.reset_mock()
                self.execute(agent, 'r', 'read_page', url=URL)
                self.reader.assert_called_once_with(URL)
                self.assertTrue(any(e['action'] == 'read_error' for e in agent.session['supply_cache_events']))

    def test_write_failure_does_not_repeat_or_fail_document_save(self):
        self.execute(self.agent, 'r', 'read_page', url=URL)
        self.execute(self.agent, 'e', 'record_evidence', source_id='S1', quote='Open daily.', claim='Hours', kind='hours')
        with patch.object(SupplyCache, '_store', side_effect=OSError('offline write failure')):
            saved = self.execute(self.agent, 's', 'save_handbook', markdown=handbook('# Offline'))
            replay = self.execute(self.agent, 's', 'save_handbook', markdown=handbook('# Offline'))
        self.assertTrue(saved['document_saved'])
        self.assertEqual(saved, replay)
        self.assertEqual(self.agent.documents.current()['version'], 1)
        self.assertTrue(any(e['action'] == 'write_error' for e in self.agent.session['supply_cache_events']))

    def test_conditions_destination_and_revision_isolation(self):
        self.seed()
        agents = [self.new('changed', pace='slow'), self.new('different'), self.new('revision')]
        agents[1].session['intake']['destination'] = 'Different City'
        agents[2].session['messages'].append(dict(role='user', content='Change dates'))
        for agent in agents:
            self.reader.reset_mock()
            self.execute(agent, 'r', 'read_page', url=URL)
            self.reader.assert_called_once_with(URL)

    def test_rejected_save_does_not_cache(self):
        self.execute(self.agent, 'r', 'read_page', url=URL)
        self.execute(self.agent, 'e', 'record_evidence', source_id='S1', quote='Open daily.', claim='Hours', kind='hours')
        result = self.execute(self.agent, 's', 'save_handbook', markdown='')
        self.assertTrue(result['not_completed'])
        self.assertEqual(SupplyCache(self.agent.supply.path).read_destination('Synthetic City'), [])
