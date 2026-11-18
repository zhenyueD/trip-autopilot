"""Budget-aware fake HTTP completion through the real save/render path."""
import asyncio
from first_eye_fixtures import with_budget
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from packages.travel_agent import single_session_agent as module
from packages.travel_agent.comparison_card_budget import BudgetExhausted
from packages.travel_agent.model_sizing import baseline_entry
from packages.travel_agent.writer_tokenizer import count_writer_text
from tests.test_single_session_destination_detail import fixture, markdown
from tests.test_single_session_destination_context import fixture as overview
from tools.build_session_sizing_baseline import build, STAGE


class SessionSizingTests(unittest.TestCase):
    def test_measured_registry_and_payload(self):
        self.assertEqual(build(), baseline_entry(STAGE))
        with tempfile.TemporaryDirectory() as directory:
            agent = module.SingleSessionAgent(directory)
            self.assertEqual(build()['recommended_max_tokens'], agent.payload()['max_tokens'])
            agent.session['configuration']['max_tokens'] = 24000
            agent.persist()
            restored = module.SingleSessionAgent(directory)
            self.assertEqual(restored.payload()['max_tokens'], restored.session['configuration']['max_tokens'])

    def run_completion(self, cap=None, force_length=False):
        with tempfile.TemporaryDirectory() as directory:
            # This fixture asserts the historical finite-role recovery contract.
            # Unlimited new tasks are covered independently by allowance tests.
            with patch.dict(os.environ, {'TRAVEL_SESSION_MAX_REQUESTS': '16'}):
                agent = module.SingleSessionAgent(directory)
            agent.documents.register_source('s', 'https://example.test', 'CNY 80', '2026-11-05')
            agent.documents.record_evidence('s', 'CNY 80', 'synthetic price', 'price', 80, 'CNY', 2)
            data = fixture()
            day = data['days'][0]
            data['days'] = []
            for i in range(6):
                item = deepcopy(day)
                item.update(id=f'day-{i+1}', title=f'离线第{i+1}天', date=f'2026-12-{5+i:02d}')
                data['days'].append(item)
            data['overview'] = overview()
            prose = '# 离线合成长手册\n\n{{price:E1}}\n'
            count = lambda s: count_writer_text(s, model=module.MODEL)
            while True:
                document = with_budget(markdown(data, prose))
                arguments = json.dumps({'markdown': document}, ensure_ascii=False)
                visible = count(arguments)
                if visible >= 11015:
                    break
                prose += '合成旅行安排说明，保留休息与待确认事项。\n' * 20
            required = 16556 + visible
            truncated_scenario = force_length or (cap is not None and cap < required)
            expected_writer_calls = 4 if truncated_scenario else 1
            seen, roles = [], []
            # run_turn now always uses isolated roles. Supply the existing
            # synthetic source's inventory so research can yield in one call;
            # this fixture measures writer sizing, not research starvation.
            intake = {'case_id': 'offline', 's1_price_items': [
                {'id': 'ticket', 'label': 'synthetic price', 'conditions': 'two synthetic adults'}]}

            def respond(request):
                payload = json.loads(request.content)
                names = {t['function']['name'] for t in payload['tools']}
                if 'declare_price_inventory' in names:
                    roles.append('research')
                    content = json.dumps({'price_coverage': [
                        {'item_id': 'ticket', 'source_id': 's', 'quote': 'CNY 80'}]})
                    return module.httpx.Response(200, json=dict(id='offline-research', choices=[dict(
                        message=dict(role='assistant', content=content), finish_reason='stop')]))
                if 'save_handbook' not in names:
                    roles.append('adjudicator')
                    content = json.dumps({'decisions': [{'candidate_id': 's', 'status': 'accepted',
                                         'reason': 'existing synthetic evidence'}]})
                    return module.httpx.Response(200, json=dict(id='offline-role', choices=[dict(
                        message=dict(role='assistant', content=content), finish_reason='stop')]))
                roles.append('writer')
                seen.append(payload)
                self.assertLessEqual(len(seen), expected_writer_calls)
                if not truncated_scenario or force_length:
                    self.assertGreaterEqual(payload['max_tokens'], required)
                # The old cap under-sizes the first high-reasoning response.
                # Length scenarios then deliberately keep returning incomplete
                # output through the real bounded recovery, even with its larger
                # high-thinking allowance; this is persistent fault injection.
                truncated = truncated_scenario
                reasoning = 16556 if payload['reasoning_effort'] == 'high' else 0
                message = dict(role='assistant', content=None,
                               reasoning_content='offline synthetic reasoning' if reasoning else '',
                               tool_calls=[dict(id='save-' + str(len(seen)), type='function', function=dict(
                                   name='save_handbook', arguments=arguments if not truncated else arguments[:200]))])
                return module.httpx.Response(200, json=dict(id='offline-sizing',
                    choices=[dict(message=message, finish_reason='length' if truncated else 'tool_calls')],
                    usage=dict(prompt_tokens=100, completion_tokens=min(reasoning + visible, payload['max_tokens']),
                               completion_tokens_details=dict(reasoning_tokens=reasoning))))

            original = module.NativeRecordedTransport
            def transport(*args):
                return original(*args, inner=module.httpx.MockTransport(respond))

            with patch.object(module, 'NativeRecordedTransport', side_effect=transport), \
                    patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'offline-fake'}), \
                    patch.object(module, 'MAX_TOKENS', cap or module.MAX_TOKENS):
                if truncated_scenario:
                    if cap is not None:
                        self.assertGreater(required, cap)
                    with self.assertRaisesRegex(BudgetExhausted, 'Writer truncation recovery exhausted'):
                        asyncio.run(agent.run_turn('offline', intake=intake))
                    self.assertIsNone(agent.documents.current())
                    self.assertEqual([], agent.session['tools'])
                    recovery = json.loads((Path(directory) / 'roles/writer/recovery.json').read_text())
                    self.assertEqual(recovery['status'], 'exhausted')
                    self.assertEqual(len(recovery['attempts']), 4)
                    self.assertTrue(all(attempt['accounted'] for attempt in recovery['attempts']))
                    for attempt in recovery['attempts']:
                        raw = json.loads(Path(attempt['artifact']).read_text())
                        self.assertEqual(raw['finish_reason'], 'length')
                        self.assertEqual(raw['message']['tool_calls'][0]['function']['arguments'], arguments[:200])
                    transcript = json.loads((Path(directory) / 'roles/writer/messages.json').read_text())
                    self.assertFalse(any(message.get('tool_calls') for message in transcript))
                    with self.assertRaisesRegex(RuntimeError, 'Role generation incomplete'):
                        asyncio.run(agent.run_turn('retry'))
                    self.assertEqual(agent.session['turns'][-1]['status'], 'failed')
                else:
                    saved = asyncio.run(agent.run_turn('offline', intake=intake))
                    self.assertTrue(saved['document_saved'])
                    self.assertEqual(agent.documents.current()['markdown'], document)
                    page = Path(saved['html_path']).read_text()
                    for title in ['离线第6天', '这趟怎么定下来的', '预算分档与总额', '节奏', '时差与当地作息', '160 元']:
                        self.assertIn(title, page)
                    self.assertEqual(len(agent.documents.evidence), 1)
                    self.assertEqual(agent.session['turns'][-1]['status'], 'completed')
                self.assertEqual(roles, ['research', 'adjudicator'] + ['writer'] * expected_writer_calls)
                self.assertEqual(len(seen), expected_writer_calls)
                # RevisionRepairTests also fixes recovery at high/enabled/60000;
                # the legacy recovery key does not request disabled thinking.
                self.assertEqual([payload['reasoning_effort'] for payload in seen],
                                 ['high'] * expected_writer_calls)
                self.assertEqual([payload['thinking']['type'] for payload in seen],
                                 ['enabled'] * expected_writer_calls)
                self.assertEqual([payload['max_tokens'] for payload in seen],
                                 [cap or module.MAX_TOKENS] + [60000] * (expected_writer_calls - 1))
                expected_calls = 2 + expected_writer_calls
                self.assertEqual(agent.budget.snapshot()['used'], expected_calls)
                state = json.loads((Path(directory) / 'roles/state.json').read_text())
                self.assertEqual([state['roles'][role]['accounting']['used'] for role in
                                  ('research', 'adjudicator', 'writer')], [1, 1, expected_writer_calls])
                for index in range(3, expected_calls + 1):
                    receipt = json.loads((Path(directory) / f'model-artifacts/model-{index:03d}-response.json').read_text())
                    self.assertEqual(receipt['http_status'], 200)
                    response = json.loads(receipt['body_utf8'])
                    self.assertEqual(response['choices'][0]['finish_reason'],
                                     'length' if truncated_scenario else 'tool_calls')
                if os.environ.get('SESSION_SIZING_EVIDENCE_DIR'):
                    label = 'forced-length' if force_length else str(seen[0]['max_tokens'])
                    destination = Path(os.environ['SESSION_SIZING_EVIDENCE_DIR']) / label
                    shutil.copytree(directory, destination, dirs_exist_ok=True)
                print(f'OFFLINE_SIZING cap={seen[0]["max_tokens"]} visible={visible} reasoning=16556 required={required} status={agent.session["turns"][-1]["status"]} fake_http_calls={expected_calls} persistent_length_injection={truncated_scenario} product_calls=0')

    def test_long_high_reasoning_completion_saves_all_days_and_overview(self):
        self.run_completion()

    def test_old_cap_and_persistent_length_exhaust_recovery_without_save(self):
        self.run_completion(cap=24000)

    def test_unexpected_length_at_new_cap_still_fails_closed(self):
        self.run_completion(force_length=True)
