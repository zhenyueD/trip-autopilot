"""Offline tool scheduling/cache regression; all network/model entry points stubbed."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from packages.travel_agent.single_session_agent import SingleSessionAgent

MODULE = 'packages.travel_agent.single_session_agent'
URL = 'https://example.test/admission'


def call(i, name, **args):
    return dict(id=str(i), type='function', function=dict(name=name, arguments=json.dumps(args)))


def capture(url):
    return dict(status='read', url=url, text='Admission THB 120. Open daily.',
                fetched_at='2026-11-05T08:00:00+00:00')


class AgentToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.agent = SingleSessionAgent(Path(self.temp.name) / 'session')
        self.agent.session['intake'] = {'case_id': 'offline'}
        # An unstubbed provider/reader call is always a test error.
        for target in ('_model', '_search'):
            p = patch.object(self.agent, target, side_effect=AssertionError('external call'))
            p.start()
            self.addCleanup(p.stop)
        p = patch(MODULE + '.capture_jina_page', side_effect=AssertionError('external read'))
        p.start()
        self.addCleanup(p.stop)

    def execute(self, i, name, **args):
        return self.agent._execute_tool(call(i, name, **args))

    def test_parallel_timing_and_run_turn_message_order(self):
        delays = [.24, .16, .08]
        calls = [call(i, 'search', query=str(i), context='offline') for i in range(3)]
        completed = []

        def slow(args, request_id):
            time.sleep(delays[int(request_id)])
            completed.append(request_id)
            return {'value': args['query']}

        start = time.monotonic()
        for c in calls:
            slow(json.loads(c['function']['arguments']), c['id'])
        serial = time.monotonic() - start
        completed.clear()
        replies = [({'role': 'assistant', 'content': None, 'tool_calls': calls}, 'tool_calls'),
                   ({'role': 'assistant', 'content': None, 'tool_calls': [
                       call('save', 'save_handbook', markdown='# Offline')]}, 'tool_calls')]

        async def model():
            return replies.pop(0)

        with patch.object(self.agent, '_model', side_effect=model), \
                patch.object(self.agent, '_search', side_effect=slow):
            start = time.monotonic()
            asyncio.run(self.agent.run_turn('test', intake={'case_id': 'offline'}))
            parallel = time.monotonic() - start
        self.assertLess(parallel, serial * .8)
        self.assertEqual(completed, ['2', '1', '0'])
        messages = [m for m in self.agent.session['messages'] if m['role'] == 'tool']
        self.assertEqual([m['tool_call_id'] for m in messages], ['0', '1', '2', 'save'])
        self.assertEqual([json.loads(m['content'])['value'] for m in messages[:3]], ['0', '1', '2'])
        self.assertEqual(self.agent.budget.snapshot()['used'], 0)
        print(f'SPEED_TOOLS serial={serial:.4f}s parallel={parallel:.4f}s speedup={serial/parallel:.2f}x')

    def test_read_cache_reuses_capture_and_survives_same_session_restart(self):
        with patch(MODULE + '.capture_jina_page', side_effect=capture) as reader:
            first = self.execute('read1', 'read_page', url=URL)
            second = self.execute('read2', 'read_page', url=URL)
            restarted = SingleSessionAgent(self.agent.directory)
            third = restarted._execute_tool(call('read3', 'read_page', url=URL))
            self.assertEqual(reader.call_count, 1)
        self.assertFalse(first['cached'])
        for result in (second, third):
            self.assertTrue(result['cached'])
            self.assertEqual(result['source_id'], first['source_id'])
            self.assertEqual(result['first_read_at'], first['retrieved_at'])
            self.assertEqual(result['content'], first['content'])
        self.assertEqual(len(self.agent.documents.sources), 1)

    def test_parallel_same_url_single_request_different_urls_unique_sources(self):
        def slow(url):
            time.sleep(.04)
            return capture(url)
        calls = [call(i, 'read_page', url=url) for i, url in enumerate([URL, URL, URL + '/other'])]
        with patch(MODULE + '.capture_jina_page', side_effect=slow) as reader:
            results = asyncio.run(self.agent._execute_calls(calls))
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(results[0]['source_id'], results[1]['source_id'])
        self.assertNotEqual(results[0]['source_id'], results[2]['source_id'])
        self.assertEqual(sum(r['cached'] for r in results), 1)
        self.assertEqual(len(self.agent.documents.sources), 2)
        self.assertEqual(len(json.loads(self.agent.path.read_text())['tools']), 3)

    def test_failed_reads_not_cached_and_sessions_isolated(self):
        failed = dict(status='blocked', reason='wall', fallback_forbidden=True)
        with patch(MODULE + '.capture_jina_page', side_effect=[failed, capture(URL), capture(URL)]) as reader:
            self.assertFalse(self.execute('r1', 'read_page', url=URL)['evidence_available'])
            self.assertFalse(self.execute('r2', 'read_page', url=URL)['cached'])
            other = SingleSessionAgent(Path(self.temp.name) / 'other')
            self.assertFalse(other._execute_tool(call('r3', 'read_page', url=URL))['cached'])
            self.assertEqual(reader.call_count, 3)

    def test_evidence_duplicate_url_quote_reuses_id_and_ledger(self):
        for sid in ('s1', 's2'):
            self.agent.documents.register_source(sid, URL, 'Open daily. Closed never.', '2026-11-05')
        args = dict(source_id='s1', quote='Open daily.', claim='Hours', kind='hours')
        first = self.execute('e1', 'record_evidence', **args)
        ledger_path = self.agent.documents.directory / 'ledger.json'
        before = ledger_path.read_bytes()
        second = self.execute('e2', 'record_evidence', **dict(args, source_id='s2'))
        self.assertEqual(first, second)
        self.assertEqual(before, ledger_path.read_bytes())
        restarted = SingleSessionAgent(self.agent.directory)
        self.assertEqual(restarted._execute_tool(call('e3', 'record_evidence', **args)), first)
        other = self.execute('e4', 'record_evidence', **dict(args, quote='Closed never.'))
        self.assertEqual(other['evidence_id'], 'E2')
        self.assertEqual(len(self.agent.documents.evidence), 2)

    def test_mutation_barriers_preserve_read_record_save_order(self):
        calls = [call('r', 'read_page', url=URL),
                 call('e1', 'record_evidence', source_id='S1', quote='Open daily.', claim='Hours', kind='hours'),
                 call('e2', 'record_evidence', source_id='S1', quote='Open daily.', claim='Hours', kind='hours'),
                 call('s', 'save_handbook', markdown='Hours {{evidence:E1}}')]
        with patch(MODULE + '.capture_jina_page', side_effect=capture):
            results = asyncio.run(self.agent._execute_calls(calls))
        self.assertEqual(results[1]['evidence_id'], results[2]['evidence_id'])
        self.assertTrue(results[-1]['document_saved'])
        self.assertEqual(len(self.agent.documents.evidence), 1)

    def test_search_not_cached_and_duplicate_tool_id_not_repeated(self):
        with patch.object(self.agent, '_search', return_value={'ok': True}) as search:
            calls = [call(i, 'search', query='same', context='same') for i in ('1', '2', '2')]
            results = asyncio.run(self.agent._execute_calls(calls))
            self.assertEqual(search.call_count, 2)
        self.assertEqual(results, [{'ok': True}] * 3)
        with self.assertRaisesRegex(RuntimeError, 'reused'):
            self.execute('2', 'search', query='changed', context='same')

    def test_failed_batch_drains_workers_before_return(self):
        finished = []
        def search(args, request_id):
            if request_id == 'fail':
                raise RuntimeError('offline failure')
            time.sleep(.08)
            finished.append(request_id)
            return {'ok': True}
        with patch.object(self.agent, '_search', side_effect=search):
            results = asyncio.run(self.agent._execute_calls([
                call(i, 'search', query='q', context='c') for i in ('fail', 'slow')]))
        self.assertIsInstance(results[0], RuntimeError)
        self.assertEqual(finished, ['slow'])
        self.assertEqual(self.agent.session['tools'][1]['status'], 'completed')

    def test_legacy_session_read_receipts_reused_without_cache_field(self):
        with patch(MODULE + '.capture_jina_page', side_effect=capture) as reader:
            first = self.execute('legacy', 'read_page', url=URL)
            del self.agent.session['read_cache']
            self.agent.persist()
            restored = SingleSessionAgent(self.agent.directory)
            result = restored._execute_tool(call('new', 'read_page', url=URL))
        self.assertEqual(reader.call_count, 1)
        self.assertTrue(result['cached'])
        self.assertEqual(first['source_id'], result['source_id'])

    def test_duplicate_evidence_does_not_bypass_input_validation(self):
        self.agent.documents.register_source('s', URL, 'THB 120', '2026-11-05')
        args = dict(source_id='s', quote='THB 120', claim='Ticket', kind='price',
                    unit_amount='120', currency='THB', quantity=2)
        self.execute('good', 'record_evidence', **args)
        for i, override in enumerate(({'quantity': 0}, {'unit_amount': '12'},
                                      {'claim': ''}, {'kind': 'hours'})):
            result = self.execute('bad' + str(i), 'record_evidence', **dict(args, **override))
            self.assertTrue(result['not_completed'])
        self.assertEqual(len(self.agent.documents.evidence), 1)


if __name__ == '__main__':
    unittest.main()
