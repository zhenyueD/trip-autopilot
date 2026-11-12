"""Budget-aware fake HTTP completion through the real save/render path."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from packages.travel_agent import single_session_agent as module
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
                document = markdown(data, prose)
                arguments = json.dumps({'markdown': document}, ensure_ascii=False)
                visible = count(arguments)
                if visible >= 11015:
                    break
                prose += '合成旅行安排说明，保留休息与待确认事项。\n' * 20
            required = 16556 + visible
            seen = []

            def respond(request):
                payload = json.loads(request.content)
                seen.append(payload)
                truncated = force_length or payload['max_tokens'] < required
                message = dict(role='assistant', content=None, reasoning_content='offline synthetic reasoning',
                               tool_calls=[dict(id='save', type='function', function=dict(
                                   name='save_handbook', arguments=arguments if not truncated else arguments[:200]))])
                return module.httpx.Response(200, json=dict(id='offline-sizing',
                    choices=[dict(message=message, finish_reason='length' if truncated else 'tool_calls')],
                    usage=dict(prompt_tokens=100, completion_tokens=min(required, payload['max_tokens']),
                               completion_tokens_details=dict(reasoning_tokens=16556))))

            original = module.NativeRecordedTransport
            def transport(*args):
                return original(*args, inner=module.httpx.MockTransport(respond))

            with patch.object(module, 'NativeRecordedTransport', side_effect=transport), \
                    patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'offline-fake'}), \
                    patch.object(module, 'MAX_TOKENS', cap or module.MAX_TOKENS):
                if force_length or (cap and cap < required):
                    with self.assertRaisesRegex(RuntimeError, 'truncated'):
                        asyncio.run(agent.run_turn('offline', intake={'case_id': 'offline'}))
                    self.assertIsNone(agent.documents.current())
                    with self.assertRaisesRegex(RuntimeError, 'Prior turn incomplete'):
                        asyncio.run(agent.run_turn('retry'))
                    self.assertEqual(agent.session['turns'][-1]['status'], 'failed')
                else:
                    saved = asyncio.run(agent.run_turn('offline', intake={'case_id': 'offline'}))
                    self.assertTrue(saved['document_saved'])
                    self.assertEqual(agent.documents.current()['markdown'], document)
                    page = Path(saved['html_path']).read_text()
                    for title in ['离线第6天', '这趟怎么定下来的', '预算分档对照', '节奏', '时差与当地作息', '160 元']:
                        self.assertIn(title, page)
                    self.assertEqual(len(agent.documents.evidence), 1)
                    self.assertEqual(agent.session['turns'][-1]['status'], 'completed')
                self.assertEqual(len(seen), 1)
                self.assertEqual(agent.budget.snapshot()['used'], 1)
                receipt = json.loads((Path(directory) / 'model-artifacts/model-001-response.json').read_text())
                self.assertEqual(receipt['http_status'], 200)
                if os.environ.get('SESSION_SIZING_EVIDENCE_DIR'):
                    label = 'forced-length' if force_length else str(seen[0]['max_tokens'])
                    destination = Path(os.environ['SESSION_SIZING_EVIDENCE_DIR']) / label
                    shutil.copytree(directory, destination, dirs_exist_ok=True)
                print(f'OFFLINE_SIZING cap={seen[0]["max_tokens"]} visible={visible} reasoning=16556 required={required} status={agent.session["turns"][-1]["status"]} fake_http_calls=1 product_calls=0')

    def test_long_high_reasoning_completion_saves_all_days_and_overview(self):
        self.run_completion()

    def test_old_cap_reproduces_length_without_save_or_retry(self):
        self.run_completion(cap=24000)

    def test_unexpected_length_at_new_cap_still_fails_closed(self):
        self.run_completion(force_length=True)
