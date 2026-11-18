"""Offline tool scheduling/cache regression; all network/model entry points stubbed."""
import asyncio
from copy import deepcopy
from first_eye_fixtures import with_budget
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
    def test_price_removal_schema_matches_optional_nonempty_runtime_contract(self):
        from packages.travel_agent.single_session_agent import TOOLS
        from packages.travel_agent.single_session_edit_binding import declared_removal_ids
        self.assertEqual(set(), declared_removal_ids(None))
        with self.assertRaisesRegex(ValueError, 'nonempty list'):
            declared_removal_ids({'removed_prices': []})
        declaration = {'removed_prices': [{'evidence_id': 'E1', 'reason': 'Cancelled',
                                           'request_quote': 'Skip this paid visit'}]}
        self.assertEqual({'E1'}, declared_removal_ids(declaration))
        for tool in TOOLS:
            if tool['function']['name'] not in {'save_handbook', 'edit_handbook'}:
                continue
            with self.subTest(tool=tool['function']['name']):
                schema = tool['function']['parameters']
                self.assertNotIn('edit_intent', schema['required'])
                removals = schema['properties']['edit_intent']['properties']['removed_prices']
                self.assertEqual(1, removals['minItems'])
                self.assertIn('省略整个edit_intent', tool['function']['description'])

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

    def revision_base(self):
        saved = self.agent.documents.save(with_budget('# Original plan'))
        self.agent.session.update(messages=[{'role': 'system', 'content': 'Offline system'},
            {'role': 'user', 'content': 'Keep the plan'}], turns=[{'id': 1, 'status': 'completed'}])
        return saved

    def rejected_edit(self, ident):
        return call(ident, 'edit_handbook', base_version=1,
            edits=[{'old': 'absent fragment', 'new': 'replacement'}],
            receipt={k: 'offline' for k in ('changes', 'judgment', 'alternatives', 'next_action')})

    def test_three_mixed_schema_and_tool_rejections_stop_without_fourth_request(self):
        saved = self.revision_base()
        before = {p.name: p.read_bytes() for p in self.agent.documents.directory.iterdir() if p.is_file()}
        seen = []
        async def model():
            index = len(seen) + 1
            self.assertLessEqual(index, 3, 'fourth model request must never happen')
            tool = self.rejected_edit('rejected-' + str(index))
            if index in (1, 3):
                tool['function']['arguments'] = '{'
            data = {'choices': [{'finish_reason': 'tool_calls', 'message': {
                'role': 'assistant', 'content': None, 'tool_calls': [tool]}}]}
            seen.append(deepcopy(data))
            accepted, rejected = self.agent._accept_response(data, 'offline-' + str(index))
            return accepted, 'tool_calls_degraded' if rejected else 'tool_calls'
        with patch.object(self.agent, '_model', side_effect=model):
            with self.assertRaisesRegex(RuntimeError, 'No progress after 3 consecutive rejected'):
                asyncio.run(self.agent.run_turn('Adjust the saved plan'))
        self.assertEqual(3, len(seen))
        self.assertEqual('failed', self.agent.session['turns'][-1]['status'])
        self.assertEqual(3, self.agent.session['turns'][-1]['consecutive_no_progress'])
        self.assertEqual(saved['hash'], self.agent.documents.current()['hash'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.agent.documents.directory.iterdir() if p.is_file()})
        self.assertEqual(2, len(self.agent.session['tool_call_rejections']))
        self.assertEqual('rejected', self.agent.session['tools'][-1]['status'])

    def test_successful_read_resets_rejections_and_valid_edit_can_finish(self):
        self.revision_base()
        self.agent.documents.register_source('S1', URL, 'Exact source', '2026-11-17')
        seen, counters = [], []
        async def model():
            index = len(seen) + 1
            self.assertLessEqual(index, 6)
            counters.append(self.agent.session['turns'][-1].get('consecutive_no_progress', 0))
            tool = (call('successful-read', 'read_page', url=URL) if index == 3
                    else self.rejected_edit('rejected-' + str(index)))
            if index == 6:
                arguments = json.loads(tool['function']['arguments'])
                arguments['edits'] = [{'old': '# Original plan', 'new': '# Updated plan'}]
                tool['function']['arguments'] = json.dumps(arguments)
            seen.append(tool)
            return {'role': 'assistant', 'content': '', 'tool_calls': [tool]}, 'tool_calls'
        with patch.object(self.agent, '_model', side_effect=model), patch.object(
                self.agent, '_read', return_value={'status': 'read', 'source_id': 'S1', 'content': 'Exact source'}):
            result = asyncio.run(self.agent.run_turn('Adjust the saved plan'))
        self.assertEqual([0, 1, 2, 0, 1, 2], counters)
        self.assertTrue(result['document_saved'])
        self.assertEqual(2, result['version'])
        self.assertEqual(0, self.agent.session['turns'][-1]['consecutive_no_progress'])

    def test_repeated_cached_read_stops_and_restart_cannot_reset_progress(self):
        saved = self.revision_base()
        self.agent.documents.register_source('S1', URL, 'Exact source', '2026-11-17')
        self.agent.session['read_cache'] = {URL: {'status': 'read', 'source_id': 'S1',
                                                'content': 'Exact source'}}
        seen = []
        async def model():
            seen.append(len(seen) + 1)
            self.assertLessEqual(len(seen), 4)
            return {'role': 'assistant', 'content': '', 'tool_calls': [
                call('new-id-' + str(len(seen)), 'read_page', url=URL)]}, 'tool_calls'
        with patch.object(self.agent, '_model', side_effect=model):
            with self.assertRaisesRegex(RuntimeError, 'No progress after 3'):
                asyncio.run(self.agent.run_turn('Revise with this source'))
        self.assertEqual(4, len(seen))  # first read is useful; three identical reads are not
        self.assertEqual(saved['hash'], self.agent.documents.current()['hash'])
        turn = self.agent.session['turns'][-1]
        self.assertEqual(3, turn['consecutive_no_progress'])
        self.assertIn('read_page', turn['last_progress_observation']['repeated_tools'])
        self.assertTrue(any('连续两轮' in str(row.get('content')) for row in self.agent.session['messages']))
        restarted = SingleSessionAgent(self.agent.directory)
        with patch.object(restarted, '_model', side_effect=AssertionError('no extra model request')):
            with self.assertRaisesRegex(RuntimeError, 'Only a proven unsent'):
                asyncio.run(restarted.run_turn(resume_preflight=True))
        self.assertEqual(turn['effective_progress'], restarted.session['turns'][-1]['effective_progress'])

    def test_failed_read_with_fresh_ids_does_not_count_as_progress(self):
        saved = self.revision_base()
        seen = []
        async def model():
            seen.append(len(seen) + 1)
            self.assertLessEqual(len(seen), 3)
            return {'role': 'assistant', 'content': '', 'tool_calls': [
                call('failed-' + str(len(seen)), 'read_page', url=URL)]}, 'tool_calls'
        with patch.object(self.agent, '_model', side_effect=model), patch.object(
                self.agent, '_read', return_value={'status': 'unavailable', 'reason': 'HTTP 403'}):
            with self.assertRaisesRegex(RuntimeError, 'No progress after 3'):
                asyncio.run(self.agent.run_turn('Revise with this source'))
        self.assertEqual(3, len(seen))
        self.assertEqual(saved['hash'], self.agent.documents.current()['hash'])
        self.assertIn('HTTP 403', str(self.agent.session['turns'][-1]['last_progress_observation']))

    def test_revision_multi_read_cannot_be_counted_again_as_new_single_read(self):
        self.revision_base()
        body = 'abcdefghijklmnopqr' * 1000
        self.agent.documents.register_source('S1', URL, body, '2026-11-17')
        other_url = 'https://example.test/another-page'
        self.agent.documents.register_source('S2', other_url, body, '2026-11-17')
        self.agent.session['read_cache'] = {
            URL: {'status': 'read', 'source_id': 'S1', 'content': body},
            other_url: {'status': 'read', 'source_id': 'S2', 'content': body}}
        seen = []
        async def model():
            seen.append(len(seen) + 1)
            self.assertLessEqual(len(seen), 4)
            calls = [call('read-' + str(len(seen)), 'read_page', url=URL)]
            if len(seen) == 1:
                calls.append(call('second-source', 'read_page', url=other_url))
            return {'role': 'assistant', 'content': '', 'tool_calls': calls}, 'tool_calls'
        with patch.object(self.agent, '_model', side_effect=model):
            with self.assertRaisesRegex(RuntimeError, 'No progress after 3'):
                asyncio.run(self.agent.run_turn('Revise with this source'))
        self.assertEqual(4, len(seen))
        self.assertTrue(all(row['status'] == 'completed' for row in self.agent.session['tools']))
        self.assertFalse(self.agent.session['turns'][-1]['last_progress_observation']['progressed'])

    def test_parallel_timing_and_run_turn_message_order(self):
        delays = [.24, .16, .08]
        calls = [call(i, 'search', query=str(i), context='offline') for i in range(3)]
        completed = []

        def slow(args, request_id):
            request_id = request_id.rsplit(':', 1)[-1]
            time.sleep(delays[int(request_id)])
            completed.append(request_id)
            return {'value': args['query']}

        start = time.monotonic()
        for c in calls:
            slow(json.loads(c['function']['arguments']), c['id'])
        serial = time.monotonic() - start
        completed.clear()
        # Measure the parallel dispatcher itself; full-turn filesystem/model
        # bookkeeping is not a network-concurrency timing measurement.
        with patch.object(self.agent, '_search', side_effect=slow):
            start = time.monotonic()
            dispatched = asyncio.run(self.agent._execute_calls(calls))
            parallel = time.monotonic() - start
        self.assertLess(parallel, serial * .8)
        self.assertEqual(completed, ['2', '1', '0'])
        self.assertEqual([row['value'] for row in dispatched], ['0', '1', '2'])
        completed.clear()
        replies = [({'role': 'assistant', 'content': None, 'tool_calls': [
                       call('inventory', 'declare_price_inventory', price_items=[
                           dict(id='entry', label='Admission', conditions='adult')]), *calls[:2]]}, 'tool_calls'),
                   ({'role': 'assistant', 'content': None, 'tool_calls': [
                       call('save', 'save_handbook', markdown=with_budget('# Offline'))]}, 'tool_calls')]

        async def model():
            stage = self.agent._stage_payload.get()
            lease = self.agent.request_budget.reserve('llm', 'fixture-model-' + str(self.agent.budget.snapshot()['used']))
            self.agent.request_budget.settle(lease, status='completed')
            if stage['role'] == 'research' and len(replies) == 1:
                return {'role': 'assistant', 'content': json.dumps({'unavailable': [
                    dict(item_id='entry', reason='Offline result has no price source')]})}, 'stop'
            if stage['role'] == 'adjudicator':
                return {'role': 'assistant', 'content': '{"decisions": []}'}, 'stop'
            return replies.pop(0)

        with patch.object(self.agent, '_model', side_effect=model), \
                patch.object(self.agent, '_search', side_effect=slow):
            asyncio.run(self.agent.run_turn('test', intake={'case_id': 'offline'}))
        # The existing research protocol allows two searches per batch; its
        # rejection of extras is covered by test_researcher_read.
        self.assertEqual(sorted(completed), ['0', '1'])
        transcripts = [json.loads((self.agent.directory / 'roles' / role / 'messages.json').read_text())
                       for role in ('research', 'writer')]
        messages = [m for transcript in transcripts for m in transcript if m['role'] == 'tool']
        self.assertEqual([m['tool_call_id'].rsplit(':', 1)[-1] for m in messages], ['inventory', '0', '1', 'save'])
        self.assertEqual([json.loads(m['content'])['value'] for m in messages[1:3]], ['0', '1'])
        self.assertEqual(self.agent.budget.snapshot()['used'], 4)  # Mock model leases only.
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
                 call('s', 'save_handbook', markdown=with_budget('# 合成\nHours {{evidence:E1}}'))]
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
