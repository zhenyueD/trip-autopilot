"""Offline malformed-response boundary and actual incident replay."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from packages.travel_agent.autopilot_model import _assistant, _Rejected
from packages.travel_agent.autopilot_tokenizer import validate_messages
from packages.travel_agent.single_session_agent import SingleSessionAgent, TOOLS
from packages.travel_agent.single_session_tool_calls import session_assistant

ROOT = Path(__file__).resolve().parents[1]
INCIDENT = ROOT / 'research/session-frontend-2026-11-04/api-sessions/8befa4f8d835f2e200b6787f897efb29/session'
NAMES = {t['function']['name'] for t in TOOLS}


def call(ident='ok', name='search', **args):
    return {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


def body(calls):
    return {'choices': [{'finish_reason': 'tool_calls', 'message': {
        'role': 'assistant', 'content': None, 'tool_calls': calls}}]}


class DegradeTests(unittest.TestCase):
    def test_protocol_and_schema_bad_calls_do_not_kill_sibling(self):
        cases = [None, 7, {}, {'id': []}, call(name='unknown'),
                 call(query=3, context='c'), call(query='q'),
                 call(query='q', context='c', extra=True), call(query='x'*129, context='c'),
                 call(name='record_evidence', source_id='S1', quote='q', claim='c', kind='bad'),
                 call(name='record_evidence', source_id='S1', quote='q', claim='c', kind='price', quantity=True),
                 call(name='edit_handbook', base_hash='x', edits=[{'old': 1, 'new': 'x'}], receipt={})]
        for arguments in ('{', '[]', '{"q":NaN}', '{"q":1,"q":2}', 3, None):
            c = call(); c['function']['arguments'] = arguments; cases.append(c)
        for bad in cases:
            with self.subTest(bad=bad):
                msg, rejected = session_assistant(body([bad, call('good', query='q', context='c')]), TOOLS, set())
                self.assertEqual([c['id'] for c in msg['tool_calls']], ['good'])
                self.assertEqual(rejected[0]['ordinal'], 1)
                self.assertTrue(rejected[0]['rule'])

    def test_duplicate_ids_prior_and_same_response(self):
        calls = [call('prior', query='q', context='c'), call('fresh', query='q', context='c'),
                 call('fresh', query='q', context='c')]
        msg, rejected = session_assistant(body(calls), TOOLS, {'prior'})
        self.assertEqual(len(msg['tool_calls']), 1)
        self.assertEqual([r['rule'] for r in rejected], ['duplicate_tool_call_id']*2)

    def test_cap_uses_original_ordinal_and_strict_caller_unchanged(self):
        raw = body([call(str(i), query='q', context='c') for i in range(18)])
        raw['choices'][0]['message']['content'] = ''
        raw['choices'][0]['message']['tool_calls'][0] = None
        with self.assertRaises(_Rejected):
            _assistant(raw, NAMES, set())
        msg, rejected = session_assistant(raw, TOOLS, set())
        self.assertEqual(len(msg['tool_calls']), 15)
        self.assertEqual([r['ordinal'] for r in rejected], [1,17,18])
        self.assertIn('MAX_TOOL_CALLS=16', rejected[-1]['rule'])

    def test_all_invalid_round_continues_with_valid_protocol_and_persists(self):
        with tempfile.TemporaryDirectory() as temp:
            agent = SingleSessionAgent(temp)
            replies = [body([None, call(query=[])]), body([call('save', 'save_handbook', markdown='# Offline')])]
            async def model():
                if len(replies) == 1:
                    validate_messages(agent._messages(), NAMES)
                msg, rejected = agent._accept_response(replies.pop(0), f'offline-{len(replies)}')
                return msg, 'tool_calls_degraded' if rejected else 'tool_calls'
            with patch.object(agent, '_model', side_effect=model), patch.dict(os.environ, {'TRAVEL_ORCH': '0'}):
                result = asyncio.run(agent.run_turn('offline', intake={'case_id': 'offline'}))
            self.assertTrue(result['document_saved'])
            self.assertEqual(agent.session['turns'][-1]['status'], 'completed')
            restored = SingleSessionAgent(temp)
            self.assertEqual(len(restored.session['tool_call_rejections'][0]['rejected_calls']), 2)
            self.assertEqual(len(restored.session['tools']), 1)

    def test_stage_all_invalid_continues_and_checks_prior_provider_ids(self):
        from packages.travel_agent.single_session_orch_backend import NativeOrchestrationBackend
        with tempfile.TemporaryDirectory() as temp:
            agent = SingleSessionAgent(temp)
            replies = [body([None]), body([call('finish', 'finish_stage', result_json='{"ok":true}')])]
            async def model():
                stage = agent._stage_payload.get()
                if len(replies) == 1:
                    validate_messages(stage['messages'], {'finish_stage'})
                msg, rejects = agent._accept_response(replies.pop(0), 'stage-' + str(len(replies)))
                return msg, 'tool_calls_degraded' if rejects else 'tool_calls'
            with patch.object(agent, '_model', side_effect=model):
                result = asyncio.run(NativeOrchestrationBackend(agent)._stage('offline', 'test', {}))
            self.assertEqual(result, {'ok': True})
            token = agent._stage_payload.set({'messages': [{'tool_calls': [call('namespace:repeat')]}], 'tools': TOOLS})
            try:
                _, rejects = agent._accept_response(body([call('repeat', query='q', context='c')]), 'duplicate')
                self.assertEqual(rejects[0]['rule'], 'duplicate_tool_call_id')
            finally:
                agent._stage_payload.reset(token)

    def test_non_list_calls_degrade_without_type_error(self):
        for raw in ({'wrong': 'shape'}, 'bad', 2, False):
            msg, rejects = session_assistant(body(raw), TOOLS, set())
            self.assertFalse(msg.get('tool_calls'))
            self.assertIn('type must be array', rejects[0]['rule'])

    def test_envelope_errors_still_fatal(self):
        for change in ({'finish_reason': 'length'}, {'finish_reason': 'stop'}):
            raw = body([None]); raw['choices'][0].update(change)
            with self.assertRaises(_Rejected):
                session_assistant(raw, TOOLS, set())

    def test_incident_replay_to_document_store(self):
        artifact = INCIDENT / 'model-artifacts/model-003-response.json'
        raw = json.loads(json.loads(artifact.read_text())['body_utf8'])
        source_ledger = json.loads((INCIDENT / 'documents/ledger.json').read_text())
        output = os.environ.get('TOOLCALL_REPLAY_DIR')
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(output) if output else Path(temp)
            self.assertFalse((directory / 'conversation.json').exists(), 'Use a fresh replay directory')
            agent = SingleSessionAgent(directory)
            for sid, source in source_ledger['sources'].items():
                agent.documents.register_source(sid, source['url'], source['content'], source['retrieved_at'])
            replies = [deepcopy(raw), body([call('offline-synthetic-save', 'save_handbook',
                markdown='# Offline incident replay\n\nSynthetic continuation for format-boundary testing only. '
                         'This is not a model-authored travel deliverable.\n\n[证据 E1]')])]
            async def model():
                if len(replies) == 1:
                    validate_messages(agent._messages(), NAMES)
                msg, rejects = agent._accept_response(replies.pop(0), 'offline-' + str(len(replies)))
                return msg, 'tool_calls_degraded' if rejects and not msg.get('tool_calls') else 'tool_calls'
            unavailable = {'status': 'offline_unavailable', 'results': [], 'evidence_available': False,
                           'reason': 'No historical response exists for this incident search; network disabled'}
            with patch.object(agent, '_model', side_effect=model), \
                    patch.object(agent, '_search', return_value=unavailable) as search, \
                    patch.dict(os.environ, {'TRAVEL_ORCH': '0'}):
                result = asyncio.run(agent.run_turn('offline incident replay', intake={'case_id': 'offline'}))
            self.assertEqual(search.call_count, 2)
            self.assertEqual(len(agent.session['tools']), 17)
            self.assertTrue(result['document_saved'])
            rejects = agent.session['tool_call_rejections'][0]['rejected_calls']
            self.assertEqual([r['ordinal'] for r in rejects], [17, 18])
            evidence = {'product_calls': 0, 'network_calls': 0, 'incident_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
                        'continuation': 'explicit synthetic offline save; not actual model continuation',
                        'searches': unavailable, 'accepted_incident_calls': 16,
                        'executed_incident_calls': agent.session['tools'][:-1], 'rejected_calls': rejects,
                        'accepted_evidence_count': len(agent.documents.evidence), 'document': result,
                        'turn_status': agent.session['turns'][-1]['status']}
            (directory / 'offline-replay-result.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    unittest.main()
