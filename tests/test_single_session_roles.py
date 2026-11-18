"""Offline role isolation and starvation injection; no provider requests."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from first_eye_fixtures import with_budget
from packages.travel_agent.comparison_card_budget import BudgetExhausted, DurableBudget
from packages.travel_agent.single_session_agent import SingleSessionAgent
from packages.travel_agent.single_session_role_budget import LinkedBudget
from packages.travel_agent.single_session_roles import ALLOWED, LIMITS, role_tools


def call(identifier, name, **arguments):
    return {'id': identifier, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)}}


def reply(*calls, content=None):
    return {'role': 'assistant', 'content': content, 'tool_calls': list(calls)}, ('tool_calls' if calls else 'stop')


class RoleIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.agent = SingleSessionAgent(self.directory)

    def seed(self):
        self.agent.documents.register_source('S1', 'https://example.test/official',
            'Open daily. Full preserved source tail.', '2026-11-15T00:00:00Z')
        return self.agent.documents.record_evidence('S1', 'Open daily.', 'Hours', 'hours')

    def export(self, name):
        if os.environ.get('OFFLINE_ROLE_EVIDENCE_DIR'):
            target = Path(os.environ['OFFLINE_ROLE_EVIDENCE_DIR']) / name
            shutil.copytree(self.directory, target, dirs_exist_ok=True)
            sources = ['single_session_' + name + '.py' for name in ('agent', 'roles', 'role_budget', 'document')]
            manifest = {'kind': 'offline_fault_injection', 'real_product_calls': 0,
                'original_session_directory': str(self.directory),
                'artifact_paths': sorted(str(p.relative_to(target)) for p in target.rglob('*') if p.is_file()),
                'source_sha256': {name: hashlib.sha256((Path('packages/travel_agent') / name).read_bytes()).hexdigest()
                                  for name in sources},
                'first_eye': (self.agent.documents.current() or {}).get('first_eye')}
            (target / 'export-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

    def test_research_burned_to_zero_does_not_starve_writer(self):
        # This legacy starvation fixture explicitly tests the finite policy.
        self.agent.budget.maximum_requests = 16
        self.seed()
        research = LinkedBudget(self.directory / 'roles/research/accounting',
                                parent=self.agent.budget, maximum_requests=8)
        for i in range(8):
            lease = research.reserve('research', 'injected-' + str(i))
            research.settle(lease, status='unknown', error='injected failure')
        seen = []
        async def model():
            stage = self.agent._stage_payload.get()
            role = stage['role']
            seen.append(role)
            lease = self.agent.request_budget.reserve('llm', role)
            self.agent.request_budget.settle(lease, status='completed')
            if role == 'adjudicator':
                return reply(content=json.dumps({'decisions': [{'candidate_id': 'S1',
                    'status': 'accepted', 'reason': 'Existing verbatim ledger binding'}]}))
            self.assertEqual(role, 'writer')
            inputs = json.loads(stage['messages'][1]['content'])
            self.assertNotIn('content', inputs['files'][0]['content']['sources']['S1'])
            self.assertEqual(inputs['files'][0]['content']['evidence']['E1']['quote'], 'Open daily.')
            self.assertEqual(self.agent.documents.sources['S1']['content'],
                             'Open daily. Full preserved source tail.')
            return reply(call('save', 'save_handbook', markdown=with_budget(
                '# 离线故障注入\n\n营业依据 {{evidence:E1}}。门票与实时座位未知，出发前需确认。')))
        with patch.object(self.agent, '_model', side_effect=model), patch.object(
                self.agent, '_search', side_effect=AssertionError('No search allowed')):
            saved = asyncio.run(self.agent.run_turn('offline', intake={'case_id': 'offline'}))
        self.assertTrue(saved['document_saved'])
        self.assertEqual(seen, ['adjudicator', 'writer'])
        state = json.loads((self.directory / 'roles/state.json').read_text())
        self.assertEqual(state['roles']['research']['status'], 'failed')
        self.assertEqual([state['roles'][r]['accounting']['used'] for r in LIMITS], [8, 1, 1])
        self.assertEqual(self.agent.budget.snapshot()['used'], 10)
        self.assertIn('未知', self.agent.documents.current()['markdown'])
        self.assertEqual(self.agent.documents.current()['first_eye']['status'], 'incomplete')
        ledger = json.loads((self.directory / 'roles/evidence-ledger.json').read_text())
        self.assertIn('E1', ledger['evidence'])
        self.export('offline-research-exhausted')

    def test_all_research_failures_still_save_and_keep_unknown_usage(self):
        roles = []
        async def model():
            role = self.agent._stage_payload.get()['role']
            roles.append(role)
            lease = self.agent.request_budget.reserve('llm', role)
            if role == 'research':
                self.agent.request_budget.settle(lease, status='unknown', error='injected outage')
                raise RuntimeError('injected provider outage')
            self.agent.request_budget.settle(lease, status='completed')
            if role == 'adjudicator':
                return reply(content='{"decisions": []}')
            return reply(call('save', 'save_handbook', markdown=with_budget('# 离线\n关键事实未知，出发前核验。')))
        with patch.object(self.agent, '_model', side_effect=model):
            saved = asyncio.run(self.agent.run_turn('offline', intake={'case_id': 'offline'}))
        self.assertTrue(saved['document_saved'])
        self.assertEqual(roles, list(LIMITS))
        self.assertIsNone(self.agent.budget.snapshot()['calls'][0]['usage'])
        self.assertEqual(self.agent.documents.evidence, {})
        self.export('offline-research-outage')

    def test_tool_allowlists_and_dispatch_enforce_boundaries(self):
        for role, allowed in ALLOWED.items():
            self.assertEqual({t['function']['name'] for t in role_tools(role)}, allowed)
            token = self.agent._stage_payload.set({'role': role})
            try:
                forbidden = 'search' if role == 'writer' else 'save_handbook'
                with self.assertRaisesRegex(ValueError, 'not permitted'):
                    self.agent._execute_tool(call(role, forbidden))
                if role != 'adjudicator':
                    with self.assertRaisesRegex(ValueError, 'not permitted'):
                        self.agent._execute_tool(call(role + '-e', 'record_evidence'))
            finally:
                self.agent._stage_payload.reset(token)
        self.assertEqual(self.agent.documents.evidence, {})

    def test_adjudicator_single_search_has_no_retry(self):
        budget = LinkedBudget(self.directory / 'roles/adjudicator/accounting',
                              parent=self.agent.budget, maximum_requests=3)
        stage = {'role': 'adjudicator', 'budget': budget, 'search_count': 0}
        token = self.agent._stage_payload.set(stage)
        try:
            with patch.object(self.agent, '_search_once', side_effect=TimeoutError('offline')) as search:
                with self.assertRaises(TimeoutError):
                    self.agent._search({'query': 'q', 'context': 'c'}, 's1')
                with self.assertRaisesRegex(ValueError, 'limited to one'):
                    self.agent._search({'query': 'q', 'context': 'c'}, 's2')
                self.assertEqual(search.call_count, 1)
        finally:
            self.agent._stage_payload.reset(token)

    def test_search_internal_retries_charge_only_research_role(self):
        from packages.travel_agent import single_session_search
        budgets = {role: LinkedBudget(self.directory / 'roles' / role / 'accounting',
                   parent=self.agent.budget, maximum_requests=limit) for role, limit in LIMITS.items()}
        token = self.agent._stage_payload.set({'role': 'research', 'budget': budgets['research']})
        def failed_search(arguments, request_id):
            lease = self.agent.request_budget.reserve('research', request_id)
            self.agent.request_budget.settle(lease, status='unknown', error='injected transport timeout')
            raise TimeoutError('offline timeout')
        try:
            with patch.object(self.agent, '_search_once', side_effect=failed_search), patch.object(
                    single_session_search, 'RETRY_DELAY_SECONDS', 0):
                result = self.agent._search({'query': 'q', 'context': 'c'}, 'retry-fixture')
        finally:
            self.agent._stage_payload.reset(token)
        self.assertEqual(result['search_transport']['retry_count'], 1)
        self.assertEqual([budgets[r].snapshot()['used'] for r in LIMITS], [2, 0, 0])
        self.assertEqual(self.agent.budget.snapshot()['used'], 2)
        self.assertTrue(all(c['usage'] is None for c in budgets['research'].snapshot()['calls']))

    def test_atomic_role_limit_cannot_spend_parent_after_exhaustion(self):
        parent = DurableBudget(self.directory / 'parent', maximum_requests=16)
        research = LinkedBudget(self.directory / 'research', parent=parent, maximum_requests=8)
        def reserve(i):
            try:
                return research.reserve('research', str(i))
            except BudgetExhausted:
                return None
        with ThreadPoolExecutor(max_workers=5) as pool:
            leases = list(pool.map(reserve, range(16)))
        self.assertEqual(sum(v is not None for v in leases), 8)
        self.assertEqual(parent.snapshot()['used'], 8)
        writer = LinkedBudget(self.directory / 'writer', parent=parent, maximum_requests=4)
        for i in range(4):
            writer.reserve('llm', str(i))
        self.assertEqual(writer.snapshot()['used'], 4)
        self.assertEqual(research.snapshot()['used'], 8)
        self.assertEqual(parent.snapshot()['used'], 12)

    def test_parent_ceiling_is_atomic_and_unknown_is_not_zero(self):
        parent = DurableBudget(self.directory / 'parent', maximum_requests=1)
        writer = LinkedBudget(self.directory / 'writer', parent=parent, maximum_requests=4)
        lease = writer.reserve('llm', 'one')
        writer.record_request(lease, model='fixture')
        writer.settle(lease, status='unknown', error='offline')
        with self.assertRaises(BudgetExhausted):
            writer.reserve('llm', 'two')
        self.assertEqual(writer.snapshot()['used'], 1)
        for key in ('id', 'status', 'usage', 'requested_model', 'token_usage'):
            self.assertEqual(parent.snapshot()['calls'][0][key], writer.snapshot()['calls'][0][key])
        self.assertIsNone(parent.snapshot()['calls'][0]['token_usage']['input_tokens'])

    def test_legacy_shared_budget_starves_model_under_same_burn(self):
        # The former single-loop transport used this already-exhausted shared budget.
        shared = DurableBudget(self.directory / 'legacy', maximum_requests=8)
        for i in range(8):
            shared.reserve('research', str(i))
        with self.assertRaises(BudgetExhausted):
            shared.reserve('llm', 'writer-model')
        self.assertEqual(shared.snapshot()['used'], 8)
        (self.directory / 'legacy-control.json').write_text(json.dumps({
            'condition': 'eight research reservations consume shared ceiling of eight',
            'writer_reservation': 'BudgetExhausted', 'ledger': shared.snapshot()}, indent=2))
        self.export('offline-legacy-shared-control')

    def test_failed_roles_cannot_resume_into_legacy_unrestricted_tools(self):
        async def model():
            role = self.agent._stage_payload.get()['role']
            if role == 'writer':
                raise BudgetExhausted('injected writer exhaustion')
            return reply(content='{"decisions": []}')
        with patch.object(self.agent, '_model', side_effect=model):
            with self.assertRaises(BudgetExhausted):
                asyncio.run(self.agent.run_turn('offline', intake={'case_id': 'offline'}))
        for agent in (self.agent, SingleSessionAgent(self.directory)):
            for resume in (False,):
                with patch.object(agent, '_model', side_effect=AssertionError('must not issue request')):
                    with self.assertRaisesRegex(RuntimeError, 'inspect its checkpoint'):
                        asyncio.run(agent.run_turn('retry', resume_preflight=resume))

    def test_completed_generation_revise_retains_legacy_behavior(self):
        async def model():
            stage = self.agent._stage_payload.get()
            if stage and stage['role'] != 'writer':
                return reply(content='{"decisions": []}')
            if stage:
                return reply(call('save', 'save_handbook', markdown=with_budget('# Offline original')))
            self.assertEqual(self.agent.budget.maximum_requests, 2**63 - 1)
            self.assertIn('search', {t['function']['name'] for t in self.agent.payload()['tools']})
            return reply(call('edit', 'edit_handbook', base_version=1,
                edits=[{'old': '# Offline original', 'new': '# Offline revision'}],
                receipt=dict(changes='title', judgment='offline', alternatives='keep', next_action='review')))
        with patch.object(self.agent, '_model', side_effect=model):
            asyncio.run(self.agent.run_turn('offline', intake={'case_id': 'offline'}))
            revised = asyncio.run(self.agent.run_turn('Revise title'))
        self.assertEqual(revised['version'], 2)
        self.assertEqual(len(self.agent.session['turns']), 2)

    def test_rejected_binding_never_reaches_writer_or_rendered_appendix(self):
        self.seed()
        async def model():
            stage = self.agent._stage_payload.get()
            role = stage['role']
            if role == 'research':
                return reply(content='Research finished.')
            if role == 'adjudicator':
                return reply(content=json.dumps({'decisions': [
                    {'candidate_id': 'S1', 'status': 'rejected', 'reason': 'wrong travel conditions'}]}))
            inputs = json.loads(stage['messages'][1]['content'])
            self.assertEqual(inputs['files'][0]['content']['evidence'], {})
            self.assertEqual(inputs['files'][0]['content']['sources'], {})
            if stage['messages'][-1]['role'] == 'user':
                return reply(call('forbidden', 'save_handbook', markdown=with_budget('# Rejected {{evidence:E1}}')))
            result = json.loads(stage['messages'][-1]['content'])
            self.assertTrue(result['not_completed'])
            return reply(call('save', 'save_handbook', markdown=with_budget('# Offline unknown')))
        with patch.object(self.agent, '_model', side_effect=model):
            saved = asyncio.run(self.agent.run_turn('offline', intake={'case_id': 'offline'}))
        page = Path(saved['html_path']).read_text()
        self.assertNotIn('Open daily.', page)
        self.assertNotIn('example.test/official', page)
        archive = json.loads((self.directory / 'roles/adjudication-recorded-ledger.json').read_text())
        self.assertIn('E1', archive['evidence'])
        self.agent.documents.register_source('S2', 'https://example.test/new', 'New fact.', '2026-11-15')
        record = self.agent._record_evidence('S2', 'New fact.', 'New', 'other')
        self.assertEqual(record['evidence_id'], 'E2')

    def test_unfinished_adjudication_preserves_successful_new_and_existing_bindings(self):
        self.seed()
        self.agent.documents.register_source('S2', 'https://example.test/new', 'Unfinished fact.', '2026-11-15')
        async def model():
            stage = self.agent._stage_payload.get()
            if stage['role'] == 'research':
                return reply(content='Research finished.')
            if stage['role'] == 'adjudicator':
                if stage['messages'][-1]['role'] == 'user':
                    return reply(call('e', 'record_evidence', source_id='S2', quote='Unfinished fact.',
                                      claim='Pending', kind='other'))
                raise BudgetExhausted('injected after provisional binding')
            ledger = json.loads(stage['messages'][1]['content'])['files'][0]['content']
            self.assertEqual(set(ledger['evidence']), {'E1', 'E2'})
            self.assertEqual(ledger['evidence']['E2']['quote'], 'Unfinished fact.')
            return reply(call('save', 'save_handbook', markdown=with_budget(
                '# Offline {{evidence:E1}} {{evidence:E2}}')))
        with patch.object(self.agent, '_model', side_effect=model):
            saved = asyncio.run(self.agent.run_turn('offline', intake={'case_id': 'offline'}))
        page = Path(saved['html_path']).read_text()
        self.assertIn('Open daily.', page)
        self.assertIn('Unfinished fact.', page)

    def test_admission_keeps_raw_sources_and_does_not_reuse_middle_evidence_ids(self):
        from packages.travel_agent.single_session_roles import _admit
        for i in range(1, 4):
            sid, text = 'S' + str(i), 'Fact ' + str(i)
            self.agent.documents.register_source(sid, 'https://example.test/' + str(i), text, '2026-11-15')
            self.agent._record_evidence(sid, text, text, 'other')
        _admit(self.agent, ['E1', 'E3'], 'offline coverage test')
        self.assertEqual(set(self.agent.documents.sources), {'S1', 'S2', 'S3'})
        self.assertEqual(set(self.agent.documents.evidence), {'E1', 'E3'})
        record = self.agent._record_evidence('S2', 'Fact 2', 'Reconsidered for revision', 'other')
        self.assertEqual(record['evidence_id'], 'E4')
        self.assertEqual(self.agent.documents.evidence['E3']['quote'], 'Fact 3')


if __name__ == '__main__':
    unittest.main()
