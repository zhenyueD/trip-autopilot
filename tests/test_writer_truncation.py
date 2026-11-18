"""Offline writer fault injection through the real model/parser/accounting path."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import httpx2 as httpx
from first_eye_fixtures import with_budget
from test_single_session_destination_detail import fixture, markdown
from packages.travel_agent.autopilot_tokenizer import validate_messages, validate_tools
from packages.travel_agent.comparison_card_budget import BudgetExhausted
from packages.travel_agent.single_session_agent import SingleSessionAgent
from packages.travel_agent.single_session_detail import BLOCK
from packages.travel_agent.single_session_role_budget import LinkedBudget
from packages.travel_agent.single_session_roles import role_tools

HEADINGS = ('这趟怎么定下来的', '出行前待办与应急方案', '逐日行程', '钱', '出发前先核这几条', '核查记录')
USAGE = {'prompt_tokens': 217933, 'completion_tokens': 35000,
         'total_tokens': 252933, 'completion_tokens_details': {'reasoning_tokens': 28777}}
DATES = ('2026-11-16', '2026-11-17')


def tool(arguments, identifier='save'):
    return {'id': identifier, 'type': 'function',
            'function': {'name': 'save_handbook', 'arguments': arguments}}


def response(*, content=None, calls=None, reason='stop', usage=None):
    return {'id': 'offline-provider', 'model': 'deepseek-v4-pro',
            'choices': [{'index': 0, 'finish_reason': reason, 'message': {
                'role': 'assistant', 'content': content, 'tool_calls': calls or []}}],
            **({'usage': usage} if usage is not None else {})}


class WriterTruncationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.agent = SingleSessionAgent(self.directory)
        self.payloads = []
        self.writer_index = 0
        self.prose = '# 离线合成完整手册\n\n' + '\n\n'.join(
            '## ' + heading + '\n本节为离线合成测试内容，事实与可用性未知。' for heading in HEADINGS)
        self.detail = fixture()
        self.detail['days'][0]['date'] = DATES[0]
        second = deepcopy(self.detail['days'][0])
        second.update(id='day-2', title='第二天', date=DATES[1], summary='第二日合成庭院慢行。')
        self.detail['days'].append(second)
        self.full = with_budget(markdown(self.detail, self.prose))
        self.detail = json.loads(BLOCK.search(self.full)[1])
        complete_arguments = json.dumps({'markdown': self.full}, ensure_ascii=False)
        self.partial = complete_arguments[:complete_arguments.index(DATES[1]) + len('2026-')]
        self.intake = {'case_id': 'offline-writer-truncation',
                       'start_date': DATES[0], 'end_date': DATES[1], 's1_price_items': [
            {'id': 'synthetic-price', 'label': '合成测试项目', 'conditions': '离线合成'}]}
        for i in range(6):
            self.agent.documents.register_source('S' + str(i), 'https://example.test/source/' + str(i),
                'Offline synthetic admission 10 CNY. Not a travel fact.', '2026-11-15')
        self.agent.documents.record_evidence('S0', 'Not a travel fact.', '合成来源声明', 'other')
        self.initial_evidence = deepcopy(self.agent.documents.evidence)

    def complete(self, text=None, identifier='complete'):
        return response(calls=[tool(json.dumps({'markdown': self.full if text is None else text},
                                               ensure_ascii=False), identifier)],
                        reason='tool_calls', usage={'prompt_tokens': 100, 'completion_tokens': 200})

    def truncated(self, *, usage=USAGE):
        return response(content='', calls=[tool(self.partial, 'truncated')], reason='length', usage=usage)

    def measure(self, messages, *, tools, **kwargs):
        # Keep protocol validation and a bounded synthetic input. USAGE is the
        # provider response fixture and must not stand in for preflight sizing.
        validate_messages(messages, validate_tools(tools))
        return {'preflight_input_tokens': 12000, 'kind': 'offline_tokenizer_stub'}

    def run_case(self, writer_responses):
        def send(request):
            payload = json.loads(request.content)
            role = self.agent._stage_payload.get()['role']
            self.payloads.append((role, deepcopy(payload)))
            if role == 'research':
                data = response(content=json.dumps({'price_coverage': [
                    {'item_id': 'synthetic-price', 'source_id': 'S' + str(i), 'quote': '10 CNY'}
                    for i in range(3)]}), usage={'prompt_tokens': 10, 'completion_tokens': 10})
            elif role == 'adjudicator':
                data = response(content=json.dumps({'decisions': [
                    {'candidate_id': 'S' + str(i), 'status': 'accepted' if i == 0 else 'unknown',
                     'reason': '离线测试；仅 S0 有既存逐字绑定，其他待核'} for i in range(6)]}),
                    usage={'prompt_tokens': 20, 'completion_tokens': 20})
            else:
                self.assertLess(self.writer_index, len(writer_responses), 'unexpected writer retry')
                data = writer_responses[self.writer_index]
                self.writer_index += 1
            return httpx.Response(200, json=data)
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'offline-fixture-key'}), patch(
                'packages.travel_agent.comparison_card_model.httpx.AsyncHTTPTransport',
                side_effect=lambda **kwargs: httpx.MockTransport(send)), patch(
                'packages.travel_agent.single_session_agent.measure_native_input',
                side_effect=self.measure), patch.object(
                self.agent, '_search', side_effect=AssertionError('network/search forbidden')), patch.object(
                self.agent.documents, 'save', wraps=self.agent.documents.save) as save:
            self.save_spy = save
            return asyncio.run(self.agent.run_turn('保存完整离线合成手册', intake=self.intake))

    def ledger(self):
        return json.loads((self.directory / 'roles/state.json').read_text())

    def export(self, name):
        target = os.environ.get('OFFLINE_WRITER_EVIDENCE_DIR')
        if target:
            shutil.copytree(self.directory, Path(target) / name, dirs_exist_ok=True)

    def test_length_recovers_complete_handbook_without_executing_partial_call(self):
        saved = self.run_case([self.truncated(), self.complete()])
        self.assertTrue(saved['document_saved'])
        self.assertEqual(self.save_spy.call_count, 1)
        current = self.agent.documents.current()
        self.assertEqual(current['version'], 1)
        self.assertEqual(current['markdown'], self.full)
        for heading in HEADINGS:
            self.assertIn('## ' + heading, current['markdown'])
        saved_detail = json.loads(BLOCK.search(current['markdown'])[1])
        self.assertEqual(saved_detail, self.detail)
        self.assertEqual([day['date'] for day in saved_detail['days']], list(DATES))
        for heading in HEADINGS:
            self.assertIn('## ' + heading, self.partial)
        self.assertIn(DATES[0], self.partial)
        self.assertNotIn(DATES[1], self.partial)
        self.assertTrue(self.partial.endswith('2026-'))
        with self.assertRaises(json.JSONDecodeError):
            json.loads(self.partial)
        state = self.ledger()
        self.assertEqual(state['status'], 'completed')
        self.assertEqual([state['roles'][r]['accounting']['used'] for r in ('research', 'adjudicator', 'writer')], [1, 1, 2])
        self.assertEqual(self.agent.budget.snapshot()['used'], 4)
        self.assertEqual(self.agent.documents.evidence, self.initial_evidence)
        for role, payload in self.payloads[:3]:
            self.assertEqual(payload['thinking'], {'type': 'enabled'}, role)
            self.assertEqual(payload['reasoning_effort'], 'high', role)
        recovery = self.payloads[-1][1]
        self.assertEqual(recovery['thinking'], {'type': 'enabled'})
        self.assertEqual(recovery['reasoning_effort'], 'high')
        self.assertEqual(recovery['max_tokens'], 60000)
        for message in recovery['messages']:
            for call in message.get('tool_calls') or []:
                self.assertNotIn('truncated', call['id'])
                json.loads(call['function']['arguments'])
        recovery_messages = [message for message in recovery['messages']
                             if message['role'] == 'user'
                             and 'finish_reason=length' in message.get('content', '')]
        self.assertEqual(len(recovery_messages), 1)
        self.assertEqual(recovery_messages[0]['role'], 'user')
        self.assertIn('save_handbook', recovery_messages[0]['content'])
        artifact = json.loads((self.directory / 'model-artifacts/model-003-response.json').read_text())
        raw = json.loads(artifact['body_utf8'])
        self.assertEqual(raw['usage'], USAGE)
        self.assertEqual(raw['choices'][0]['message']['tool_calls'][0]['function']['arguments'], self.partial)
        self.assertEqual(state['roles']['writer']['accounting']['calls'][0]['usage'], USAGE)
        self.export('recovered')

    def test_reasoning_only_length_stops_without_automatic_resubmission_or_resume(self):
        from packages.travel_agent.single_session_roles import generate
        empty = response(content='', reason='length', usage={
            'prompt_tokens': 20084, 'completion_tokens': 35000,
            'completion_tokens_details': {'reasoning_tokens': 35000}})
        empty['choices'][0]['message']['reasoning_content'] = 'Private offline reasoning fixture.'
        with self.assertRaisesRegex(RuntimeError, 'role_no_progress'):
            self.run_case([empty])
        self.assertEqual(self.writer_index, 1)
        self.assertEqual(self.save_spy.call_count, 0)
        self.assertIsNone(self.agent.documents.current())
        state = self.ledger()
        recovery = state['roles']['writer']['truncation_recovery']
        self.assertEqual(recovery['status'], 'empty_output')
        raw = json.loads(Path(recovery['attempts'][0]['artifact']).read_text())
        self.assertEqual(raw['message'], empty['choices'][0]['message'])
        writer_call = state['roles']['writer']['accounting']['calls'][0]
        self.assertEqual(writer_call['usage'], empty['usage'])
        before = self.agent.budget.snapshot()['used']
        with patch.object(self.agent, '_model', side_effect=AssertionError('must not resend')):
            with self.assertRaisesRegex(RuntimeError, 'role_no_progress'):
                asyncio.run(generate(self.agent, self.intake, resume=True))
        self.assertEqual(self.agent.budget.snapshot()['used'], before)

    def test_repeated_truncation_stops_at_four_without_partial_document(self):
        # Preserve the explicit finite-budget exhaustion compatibility case.
        self.agent.budget.maximum_requests = 16
        with self.assertRaises(BudgetExhausted):
            self.run_case([self.truncated()] * 4)
        self.assertEqual(self.writer_index, 4)
        self.assertEqual(self.save_spy.call_count, 0)
        self.assertIsNone(self.agent.documents.current())
        state = self.ledger()
        self.assertEqual([state['roles'][r]['accounting']['used'] for r in ('research', 'adjudicator', 'writer')], [1, 1, 4])
        self.assertEqual(self.agent.budget.snapshot()['used'], 6)
        self.assertEqual(state['status'], 'failed')
        self.export('exhausted')

    def test_missing_usage_remains_null_after_successful_recovery(self):
        self.run_case([self.truncated(usage=None), self.complete()])
        role_call = self.ledger()['roles']['writer']['accounting']['calls'][0]
        parent_call = self.agent.budget.snapshot()['calls'][2]
        for call in (role_call, parent_call):
            self.assertIsNone(call['usage'])
            self.assertIsNone(call['token_usage']['input_tokens'])
            self.assertIsNone(call['token_usage']['output_tokens'])
        self.export('unknown-usage')

    def test_non_length_malformed_tool_still_passes_through_protocol_rejection(self):
        invalid = response(calls=[tool(self.partial, 'invalid')], reason='tool_calls')
        self.run_case([invalid, self.complete()])
        self.assertEqual(self.save_spy.call_count, 1)
        rejected = json.loads((self.directory / 'model-artifacts/model-003-tool-call-rejections.json').read_text())
        self.assertEqual(rejected['accepted_count'], 0)
        self.assertTrue(rejected['rejected_calls'])
        self.assertEqual(self.payloads[-1][1]['thinking'], {'type': 'enabled'})
        self.assertEqual(self.payloads[-1][1]['reasoning_effort'], 'high')
        self.assertEqual(self.agent.documents.current()['markdown'], self.full)
        self.export('non-length-rejection')

    def test_three_truncations_recover_on_last_writer_allowance(self):
        self.agent.budget.maximum_requests = 16
        saved = self.run_case([self.truncated()] * 3 + [self.complete()])
        self.assertTrue(saved['document_saved'])
        self.assertEqual(self.writer_index, 4)
        self.assertEqual(self.save_spy.call_count, 1)
        self.assertEqual(self.agent.documents.current()['markdown'], self.full)
        self.assertEqual(self.agent.documents.current()['version'], 1)
        state = self.ledger()
        self.assertEqual(state['status'], 'completed')
        self.assertEqual([state['roles'][r]['accounting']['used'] for r in
                          ('research', 'adjudicator', 'writer')], [1, 1, 4])
        self.assertEqual(self.agent.budget.snapshot()['used'], 6)
        self.assertEqual(self.agent.documents.evidence, self.initial_evidence)
        for role, payload in self.payloads[3:]:
            self.assertEqual(role, 'writer')
            self.assertEqual(payload['thinking'], {'type': 'enabled'})
            self.assertEqual(payload['reasoning_effort'], 'high')
            self.assertEqual(payload['max_tokens'], 60000)
        self.export('fourth-call-recovery')

    def test_parent_ceiling_stops_retry_with_writer_allowance_remaining(self):
        self.agent.budget.maximum_requests = 3
        with self.assertRaises(BudgetExhausted):
            self.run_case([self.truncated(), self.complete()])
        self.assertEqual(self.writer_index, 1)
        self.assertEqual(self.save_spy.call_count, 0)
        self.assertIsNone(self.agent.documents.current())
        state = self.ledger()
        writer = state['roles']['writer']['accounting']
        self.assertEqual(writer['used'], 1)
        self.assertEqual(writer['remaining'], 3)
        self.assertEqual(self.agent.budget.snapshot()['used'], 3)
        self.assertEqual(self.agent.budget.snapshot()['remaining'], 0)
        self.assertEqual(len(self.payloads), 3)
        self.assertEqual(state['status'], 'failed')
        self.export('parent-ceiling')

    def test_length_discards_valid_sibling_save_alongside_partial_call(self):
        prefix = tool(json.dumps({'markdown': self.full.replace(
            '离线合成完整手册', '截断响应前缀禁止保存')}, ensure_ascii=False), 'valid-prefix')
        truncated = response(content='discard-this-length-content', calls=[prefix, tool(self.partial, 'broken')],
                             reason='length', usage=USAGE)
        self.run_case([truncated, self.complete()])
        self.assertEqual(self.save_spy.call_count, 1)
        self.assertEqual(self.agent.documents.current()['version'], 1)
        self.assertEqual(self.agent.documents.current()['markdown'], self.full)
        transcript = json.dumps(self.payloads[-1][1]['messages'], ensure_ascii=False)
        for discarded in ('valid-prefix', 'broken', '截断响应前缀禁止保存', 'discard-this-length-content'):
            self.assertNotIn(discarded, transcript)
        raw = json.loads(json.loads((self.directory /
            'model-artifacts/model-003-response.json').read_text())['body_utf8'])
        self.assertEqual(raw['choices'][0]['message']['tool_calls'], [prefix, tool(self.partial, 'broken')])
        self.export('discard-valid-sibling')

    def test_recovery_rejects_missing_second_day_before_complete_resubmission(self):
        incomplete = deepcopy(self.detail)
        incomplete['days'] = incomplete['days'][:1]
        one_day = markdown(incomplete, self.prose)
        saved = self.run_case([self.truncated(), self.complete(one_day, 'missing-day'), self.complete()])
        self.assertTrue(saved['document_saved'])
        self.assertEqual(self.writer_index, 3)
        self.assertEqual(self.save_spy.call_count, 1)
        current = self.agent.documents.current()
        self.assertEqual(current['version'], 1)
        self.assertEqual(current['markdown'], self.full)
        saved_detail = json.loads(BLOCK.search(current['markdown'])[1])
        self.assertEqual(saved_detail, self.detail)
        self.assertEqual([day['date'] for day in saved_detail['days']], list(DATES))
        attempts = [item for item in self.agent.session['tools'] if item['name'] == 'save_handbook']
        self.assertEqual([item['status'] for item in attempts], ['rejected', 'completed'])
        self.assertIn('date_coverage', attempts[0]['error']['message'])
        self.assertIn(DATES[1], attempts[0]['error']['message'])
        feedback_messages = [message for message in self.payloads[-1][1]['messages']
                             if message['role'] == 'tool'
                             and message['tool_call_id'] == attempts[0]['id']]
        self.assertEqual(len(feedback_messages), 1)
        feedback = json.loads(feedback_messages[0]['content'])
        self.assertTrue(feedback['not_completed'])
        self.assertIn('date_coverage', feedback['message'])
        self.assertEqual([self.ledger()['roles'][r]['accounting']['used'] for r in
                          ('research', 'adjudicator', 'writer')], [1, 1, 3])
        self.assertEqual(self.agent.budget.snapshot()['used'], 5)
        self.export('missing-day-rejected')

    def test_non_writer_and_legacy_length_keep_existing_error_behavior(self):
        for role in ('research', 'adjudicator', None):
            with self.subTest(role=role):
                agent = SingleSessionAgent(self.directory / (role or 'legacy'))
                messages = [{'role': 'system', 'content': 'Offline protocol test'},
                            {'role': 'user', 'content': 'Return synthetic output'}]
                stage = None
                if role:
                    budget = LinkedBudget(agent.directory / 'role-accounting',
                                          parent=agent.budget, maximum_requests=3)
                    stage = {'role': role, 'budget': budget, 'messages': messages, 'tools': role_tools(role)}
                else:
                    agent.session['messages'] = messages
                    budget = agent.budget
                token = agent._stage_payload.set(stage)
                requests = []
                def send(request):
                    requests.append(json.loads(request.content))
                    return httpx.Response(200, json=response(content='', reason='length', usage=USAGE))
                try:
                    with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'offline-fixture-key'}), patch(
                            'packages.travel_agent.comparison_card_model.httpx.AsyncHTTPTransport',
                            side_effect=lambda **kwargs: httpx.MockTransport(send)), patch(
                            'packages.travel_agent.single_session_agent.measure_native_input',
                            side_effect=self.measure):
                        with self.assertRaisesRegex(RuntimeError, 'Model output truncated'):
                            asyncio.run(agent._model())
                finally:
                    agent._stage_payload.reset(token)
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]['thinking'], {'type': 'enabled'})
                self.assertEqual(requests[0]['reasoning_effort'], 'high')
                self.assertEqual(budget.snapshot()['used'], 1)
                self.assertEqual(agent.budget.snapshot()['used'], 1)
                self.assertIsNone(agent.documents.current())
                self.assertFalse((agent.directory / 'roles/writer/recovery.json').exists())
        self.export('other-roles-length')


if __name__ == '__main__':
    unittest.main()
