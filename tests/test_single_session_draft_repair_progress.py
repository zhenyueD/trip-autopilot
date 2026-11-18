"""A validated repair advances the writer; renamed or cycling errors do not."""
from copy import deepcopy
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from first_eye_fixtures import with_budget
from packages.travel_agent.single_session_agent import SingleSessionAgent
from packages.travel_agent import single_session_draft as draft
from packages.travel_agent import single_session_roles as roles
from packages.travel_agent.single_session_detail import BLOCK
from packages.travel_agent.single_session_progress import EffectiveProgress
from packages.travel_agent.single_session_role_budget import LinkedBudget
from packages.travel_agent.single_session_roles import _conversation, _record_progress, write_ledger
from test_single_session_roles import call, reply


def finding(value, **extra):
    return dict(stage='render', code='unbound_price', value=value,
                message='unbound price ' + value, **extra)


class DraftRepairProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.agent = SingleSessionAgent(Path(self.tmp.name))
        draft.capture(self.agent, '# Authored draft', {'version': 1, 'days': []})
        self.record = {}
        self.tracker = EffectiveProgress(self.agent, self.record)
        self.budget = LinkedBudget(self.agent.directory / 'roles/writer/accounting',
                                  parent=self.agent.budget, maximum_requests=2**63 - 1)
        self.state = {'roles': {'writer': {'no_progress': self.record}}}

    def feedback(self, findings, base=None):
        return dict(document_saved=False, draft_hash=base or draft.ensure(self.agent)['draft_hash'],
                    findings=deepcopy(findings), semantic_review='not_performed')

    def observe(self, tool, result):
        observation = self.tracker.observe([{'function': {'name': tool}}], [result])
        _record_progress(self.agent, 'writer', self.budget, self.state, observation, [])
        return observation

    def edit(self, findings):
        current = draft.ensure(self.agent)
        result = draft.edit(self.agent, current['draft_hash'],
                            [dict(path='/markdown', old=current['markdown'],
                                  new=current['markdown'] + '\nA local edit.')])
        with patch.object(self.agent, '_check_draft', return_value=self.feedback(findings)) as check:
            observation = self.observe('edit_draft', result)
            check.assert_called_once_with(result['draft_hash'])
        return observation

    def test_check_edit_check_does_not_kill_verified_repair(self):
        a, b = finding('2500元'), finding('80元')
        self.assertTrue(self.observe('check_draft', self.feedback([a, b]))['progressed'])
        self.assertTrue(self.edit([b])['progressed'])
        self.assertFalse(self.observe('check_draft', self.feedback([b]))['progressed'])
        self.assertEqual(1, self.record['consecutive_stagnant_rounds'])
        self.assertTrue(self.edit([])['progressed'])
        self.assertIsNone(self.agent.documents.current())
        self.assertEqual(0, self.agent.budget.snapshot()['used'])

    def test_duplicate_occurrence_removal_is_progress_not_offset_movement(self):
        a = finding('80元', location={'line': 10})
        self.observe('check_draft', self.feedback([a, a]))
        self.assertTrue(self.edit([finding('80元', location={'line': 15})])['progressed'])
        self.assertFalse(self.edit([finding('80元', location={'line': 25})])['progressed'])

    def test_error_rotation_and_new_completeness_failure_are_not_progress(self):
        a, b, c = finding('10元'), finding('20元'), finding('30元')
        self.observe('check_draft', self.feedback([a, b]))
        self.assertFalse(self.edit([b, c])['progressed'])
        self.assertFalse(self.edit([dict(stage='completeness', message='missing required days')])['progressed'])
        with self.assertRaisesRegex(RuntimeError, 'role_no_progress'):
            self.observe('check_draft', self.feedback([dict(stage='completeness', message='missing required days')]))

    def test_error_reintroduction_does_not_replenish_credit_across_restart(self):
        a, b = finding('10元'), finding('20元')
        self.observe('check_draft', self.feedback([a, b]))
        self.assertTrue(self.edit([b])['progressed'])
        self.record = json.loads(json.dumps(self.record))
        self.agent = SingleSessionAgent(self.agent.directory)
        self.state['roles']['writer']['no_progress'] = self.record
        self.tracker = EffectiveProgress(self.agent, self.record)
        self.assertFalse(self.edit([a, b])['progressed'])
        self.assertFalse(self.edit([b])['progressed'])
        with self.assertRaisesRegex(RuntimeError, 'role_no_progress'):
            self.observe('check_draft', self.feedback([b]))

    def test_stale_check_and_rejected_edit_cannot_establish_or_reset_baseline(self):
        stale = self.feedback([finding('80元')], base='stale')
        self.assertFalse(self.observe('check_draft', stale)['progressed'])
        self.assertTrue(self.observe('check_draft', self.feedback([finding('80元')]))['progressed'])
        with patch.object(self.agent, '_check_draft') as check:
            self.assertFalse(self.observe('edit_draft', ValueError('stale hash'))['progressed'])
            check.assert_not_called()
        self.assertFalse(self.observe('check_draft', stale)['progressed'])

    def test_same_draft_cannot_gain_progress_from_fabricated_clean_feedback(self):
        self.observe('check_draft', self.feedback([finding('80元')]))
        self.assertFalse(self.observe('check_draft', self.feedback([]))['progressed'])
        self.assertFalse(self.observe('check_draft', self.feedback([]))['progressed'])
        with self.assertRaisesRegex(RuntimeError, 'role_no_progress'):
            self.observe('check_draft', self.feedback([]))

    def test_failed_receipts_do_not_credit_diagnostics_or_request_local_recheck(self):
        for status in ('failed', 'rejected', 'blocked', 'unavailable', 'not_executed'):
            with self.subTest(status=status):
                tracker = EffectiveProgress(self.agent, {})
                failed = dict(self.feedback([finding('80元')]), status=status)
                for name in ('check_draft', 'edit_draft'):
                    with patch.object(self.agent, '_check_draft') as check:
                        result = tracker.observe([{'function': {'name': name}}], [failed])
                        self.assertFalse(result['progressed'])
                        self.assertNotIn('draft_validation', tracker.state)
                        check.assert_not_called()
                tracker.observe([{'function': {'name': 'check_draft'}}],
                                [self.feedback([finding('80元')])])
                with patch.object(self.agent, '_check_draft') as check:
                    self.assertFalse(tracker.observe([{'function': {'name': 'edit_draft'}}],
                                                     [failed])['progressed'])
                    check.assert_not_called()


class WriterFinalizationTests(unittest.TestCase):
    """The real role loop must reach the existing publisher after a clean repair."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.agent = SingleSessionAgent(Path(self.tmp.name))
        self.detail = json.loads(BLOCK.search(with_budget('# 离线测试'))[1])
        self.detail['days'][0]['timeline'] = [
            {'time': '09:00', 'kind': 'activity', 'action': '建议活动，条件待确认'}]
        self.clean_text = '# 离线行程\n所有费用未核验。'
        # The observed writer had already filled this day's authoring slot.
        draft.ensure(self.agent)
        draft.capture(self.agent, '# 离线行程\n' + '\n'.join(
            f'项目{i}费用{i + 10}元。' for i in range(6)), self.detail)
        write_ledger(self.agent)
        self.state = {'roles': {'writer': {}}}
        self.budget = LinkedBudget(self.agent.directory / 'roles/writer/accounting',
                                  parent=self.agent.budget, maximum_requests=2**63 - 1)
        self.steps = []

    def prepared_reply(self, step):
        current = draft.ensure(self.agent)
        base = current['draft_hash']
        if step in (0, 2, 4):
            feedback = self.agent._check_draft(base)
            self.assertEqual(6 if step == 0 else 0, len(feedback['findings']))
            self.assertEqual('not_performed', feedback['semantic_review'])
            return reply(call(str(step), 'check_draft', base_hash=base))
        if step == 1:
            return reply(call(str(step), 'edit_draft', base_hash=base, edits=[
                {'path': '/markdown', 'old': current['markdown'], 'new': self.clean_text}]))
        if step == 3:
            timeline = current['detail']['days'][0]['timeline']
            return reply(call(str(step), 'edit_draft', base_hash=base, edits=[
                {'path': '/detail/days/0/timeline', 'old': timeline, 'new': [
                    {'time': '08:30', 'kind': 'breakfast',
                     'action': '途中早餐为规划假设，餐厅及时间待确认'}] + timeline}]))
        return None

    def run_writer(self, final_reply):
        async def model():
            step = len(self.steps)
            self.steps.append(step)
            if step > 9:
                self.fail('Writer exceeded the bounded offline script')
            lease = self.budget.reserve('llm', 'offline-' + str(step))
            try:
                response = self.prepared_reply(step) or final_reply(step)
            except BaseException:
                self.budget.settle(lease, status='failed')
                raise
            self.budget.settle(lease, status='completed')
            return response
        with patch.object(self.agent, '_model', side_effect=model), patch.object(
                self.agent, '_search', side_effect=AssertionError('No network')):
            return asyncio.run(_conversation(self.agent, 'writer', self.budget,
                               {'intake': {}, 'request': 'offline semantic finalization'}, self.state))

    def assert_finalization_offer(self):
        progress = self.state['roles']['writer']['no_progress']
        self.assertEqual(2, progress['consecutive_stagnant_rounds'])
        self.assertFalse(progress['last_observation']['progressed'])
        self.assertTrue(progress['last_observation']['writer_finalization_applied'])
        finalization = progress['effective_progress']['writer_finalization']
        self.assertTrue(finalization['opportunity_used'])
        self.assertEqual('not_performed', finalization['semantic_review'])
        self.assertEqual(draft.ensure(self.agent)['draft_hash'], finalization['draft_hash'])
        instruction = self.agent._stage_payload.get()['messages'][-1]['content']
        for value in (finalization['draft_hash'], 'semantic_review=not_performed',
                      'publish_draft', '仅此一次', '语义阻塞'):
            self.assertIn(value, instruction)
        return deepcopy(finalization)

    def restart(self):
        self.state = json.loads((self.agent.directory / 'roles/state.json').read_text())
        self.state['roles']['writer']['resuming'] = True
        self.agent = SingleSessionAgent(self.agent.directory)
        self.budget = LinkedBudget(self.agent.directory / 'roles/writer/accounting',
                                  parent=self.agent.budget, maximum_requests=2**63 - 1)

    def test_clean_recheck_semantic_edit_recheck_reaches_formal_publish(self):
        def publish(step):
            self.assertEqual(5, step)
            self.assertIsNone(self.agent.documents.current())
            self.assert_finalization_offer()
            return reply(call('publish', 'publish_draft',
                              base_hash=draft.ensure(self.agent)['draft_hash']))
        result = self.run_writer(publish)
        self.assertTrue(result['document_saved'], result)
        self.assertEqual(1, result['version'])
        self.assertEqual(6, len(self.steps))
        saved = self.agent.documents.current()['markdown']
        self.assertIn('途中早餐为规划假设', saved)
        self.assertNotIn('项目0费用10元', saved)

    def test_repeated_empty_check_consumes_opportunity_then_stops(self):
        def check(step):
            self.assertEqual(5, step)
            self.assert_finalization_offer()
            return reply(call('empty-again', 'check_draft',
                              base_hash=draft.ensure(self.agent)['draft_hash']))
        with self.assertRaisesRegex(RuntimeError, 'role_no_progress'):
            self.run_writer(check)
        self.assertEqual(6, len(self.steps))
        self.assertIsNone(self.agent.documents.current())
        progress = self.state['roles']['writer']['no_progress']
        self.assertEqual(3, progress['consecutive_stagnant_rounds'])
        self.assertFalse(progress['last_observation']['writer_finalization_applied'])
        self.restart()
        with patch.object(self.agent, '_model') as model:
            with self.assertRaisesRegex(RuntimeError, 'role_no_progress'):
                asyncio.run(_conversation(self.agent, 'writer', self.budget, {}, self.state))
            model.assert_not_called()

    def test_another_wording_edit_cannot_extend_finalization(self):
        def edit(step):
            self.assert_finalization_offer()
            current = draft.ensure(self.agent)
            return reply(call('wording', 'edit_draft', base_hash=current['draft_hash'], edits=[
                {'path': '/markdown', 'old': current['markdown'],
                 'new': current['markdown'] + '\n措辞补充。'}]))
        with self.assertRaisesRegex(RuntimeError, 'role_no_progress'):
            self.run_writer(edit)
        self.assertEqual(6, len(self.steps))
        self.assertIsNone(self.agent.documents.current())

    def test_regressed_findings_do_not_receive_clean_finalization(self):
        original = self.prepared_reply
        def regressed(step):
            current = draft.ensure(self.agent)
            if step == 3:
                return reply(call('regress', 'edit_draft', base_hash=current['draft_hash'], edits=[
                    {'path': '/markdown', 'old': current['markdown'],
                     'new': current['markdown'] + '\n新增费用99元。'}]))
            if step == 4:
                return reply(call('recheck-regression', 'check_draft', base_hash=current['draft_hash']))
            return original(step)
        with patch.object(self, 'prepared_reply', side_effect=regressed):
            with self.assertRaisesRegex(RuntimeError, 'role_no_progress'):
                self.run_writer(lambda step: self.fail('Invalid draft received finalization'))
        self.assertEqual(5, len(self.steps))
        progress = self.state['roles']['writer']['no_progress']['effective_progress']
        self.assertNotIn('writer_finalization', progress)
        self.assertIsNone(self.agent.documents.current())

    def test_publish_rechecks_new_finding_after_the_opportunity_was_granted(self):
        def publish_regression(step):
            self.assert_finalization_offer()
            current = draft.ensure(self.agent)
            # A concurrent edit after the clean check must still hit the real
            # publisher's validation, even though a finalization offer exists.
            changed = draft.edit(self.agent, current['draft_hash'], [
                {'path': '/markdown', 'old': current['markdown'],
                 'new': current['markdown'] + '\n新增费用99元。'}])
            return reply(call('publish-regression', 'publish_draft', base_hash=changed['draft_hash']))
        with self.assertRaisesRegex(RuntimeError, 'role_no_progress'):
            self.run_writer(publish_regression)
        self.assertIsNone(self.agent.documents.current())
        messages = json.loads((self.agent.directory / 'roles/writer/messages.json').read_text())
        result = json.loads(next(m['content'] for m in reversed(messages) if m['role'] == 'tool'))
        self.assertTrue(result['not_completed'], result)
        self.assertIn('99', result['message'])

    def test_restart_preserves_consumed_offer_and_recovers_missing_instruction(self):
        offered = []
        def interrupt(*args):
            _record_progress(*args)
            if args[4].get('writer_finalization_applied'):
                offered.append(self.assert_finalization_offer())
                raise RuntimeError('offline interruption before final request')
        with patch.object(roles, '_record_progress', side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, 'offline interruption'):
                self.run_writer(lambda step: self.fail('Request started before checkpoint interruption'))
        path = self.agent.directory / 'roles/writer/messages.json'
        messages = json.loads(path.read_text())
        self.assertIn('最终发布收尾机会', messages[-1]['content'])
        # Emulate interruption between durable state and message checkpoint.
        path.write_text(json.dumps(messages[:-1], ensure_ascii=False))
        self.restart()
        def check(step):
            self.assertEqual(offered[0]['draft_hash'], self.assert_finalization_offer()['draft_hash'])
            return reply(call('after-restart', 'check_draft',
                              base_hash=draft.ensure(self.agent)['draft_hash']))
        with self.assertRaisesRegex(RuntimeError, 'role_no_progress'):
            self.run_writer(check)
        progress = self.state['roles']['writer']['no_progress']
        self.assertEqual(offered[0]['draft_hash'],
                         progress['effective_progress']['writer_finalization']['draft_hash'])
        self.assertTrue(progress['effective_progress']['writer_finalization']['request_started'])
        self.assertEqual(3, progress['consecutive_stagnant_rounds'])
        self.assertEqual(6, len(self.steps))

    def test_corrupt_recovery_cannot_refresh_the_consumed_offer(self):
        def interrupt(step):
            self.assert_finalization_offer()
            raise RuntimeError('offline interruption')
        with self.assertRaisesRegex(RuntimeError, 'offline interruption'):
            self.run_writer(interrupt)
        path = self.agent.directory / 'drafts/current.json'
        corrupted = json.loads(path.read_text())
        corrupted['markdown'] += '\ncorruption without hash update'
        path.write_text(json.dumps(corrupted, ensure_ascii=False))
        self.restart()
        with patch.object(self.agent, '_model') as model:
            with self.assertRaisesRegex(RuntimeError, 'role_no_progress'):
                asyncio.run(_conversation(self.agent, 'writer', self.budget, {}, self.state))
            model.assert_not_called()
        self.assertTrue(self.state['roles']['writer']['no_progress'][
            'effective_progress']['writer_finalization']['opportunity_used'])
        self.assertIsNone(self.agent.documents.current())

    def assert_final_reply_is_terminal(self, response, expected_error, error_type=RuntimeError):
        def finish(step):
            self.assertEqual(5, step, 'Finalization issued more than one model request')
            self.assert_finalization_offer()
            if isinstance(response, BaseException):
                raise response
            return response
        with self.assertRaisesRegex(error_type, expected_error):
            self.run_writer(finish)
        self.assertEqual(6, len(self.steps))
        self.restart()
        progress = self.state['roles']['writer']['no_progress']
        self.assertTrue(progress['effective_progress']['writer_finalization']['request_started'])
        self.assertEqual('stopped', progress['status'])
        self.assertIsNone(self.agent.documents.current())
        with patch.object(self.agent, '_model') as model:
            with self.assertRaisesRegex(RuntimeError, 'role_no_progress'):
                asyncio.run(_conversation(self.agent, 'writer', self.budget, {}, self.state))
            model.assert_not_called()

    def test_semantic_blocker_reply_cannot_reopen_finalization_on_resume(self):
        self.assert_final_reply_is_terminal(reply(content='语义阻塞：仍需确认跨日衔接。'),
                                            'Writer ended without saving')

    def test_forbidden_tool_cannot_reopen_finalization_on_resume(self):
        self.assert_final_reply_is_terminal(reply(call('forbidden', 'search', query='unused')),
                                            'Tool forbidden in role', ValueError)

    def test_failed_model_request_cannot_reopen_finalization_on_resume(self):
        self.assert_final_reply_is_terminal(RuntimeError('offline provider failed'),
                                            'offline provider failed')

    def test_truncated_finalization_cannot_issue_a_recovery_request(self):
        self.assert_final_reply_is_terminal(
            ({'role': 'assistant', 'content': '未完成的成文', 'tool_calls': []}, 'length'),
            'role_no_progress')

    def seed_legacy_stopped_checkpoint(self):
        # Reproduce the old counter behavior without changing production files.
        with patch('packages.travel_agent.single_session_progress.claim_writer_finalization',
                   return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'role_no_progress'):
                self.run_writer(lambda step: self.fail('Legacy loop must stop before publish'))
        maximum = 2**63 - 1
        self.state.update(status='failed', next_role='writer', stop_at=maximum,
                          limits=dict.fromkeys(roles.LIMITS, maximum), request='offline legacy resume')
        writer = self.state['roles']['writer']
        writer.update(status='failed', resuming=False, error={
            'type': 'RuntimeError', 'message':
            'role_no_progress: writer completed three rounds without new effective progress'})
        writer['no_progress']['last_observation'].pop('writer_finalization_applied', None)
        roles.private_json(self.agent.directory / 'roles/state.json', self.state)
        self.agent.persist()
        self.assertEqual('stopped', writer['no_progress']['status'])
        self.assertEqual({}, writer['no_progress']['effective_progress']['draft_validation']['best_findings'])
        self.assertNotIn('writer_finalization', writer['no_progress']['effective_progress'])

    def test_legacy_stopped_checkpoint_can_publish_through_generate_resume(self):
        self.seed_legacy_stopped_checkpoint()
        self.agent = SingleSessionAgent(self.agent.directory)
        requests = []
        async def publish():
            requests.append('publish')
            durable = json.loads((self.agent.directory / 'roles/state.json').read_text())
            finalization = durable['roles']['writer']['no_progress']['effective_progress'][
                'writer_finalization']
            self.assertTrue(finalization['request_started'])
            self.assertEqual('not_performed', finalization['semantic_review'])
            lease = self.agent.request_budget.reserve('llm', 'legacy-final-publish')
            self.agent.request_budget.settle(lease, status='completed')
            return reply(call('legacy-publish', 'publish_draft',
                              base_hash=draft.ensure(self.agent)['draft_hash']))
        with patch.object(self.agent, '_model', side_effect=publish), patch.object(
                self.agent, '_search', side_effect=AssertionError('No network')):
            result = asyncio.run(roles.generate(self.agent, {}, resume=True))
        self.assertTrue(result['document_saved'])
        self.assertEqual(1, result['version'])
        self.assertEqual(['publish'], requests)
        self.assertIn('途中早餐为规划假设', self.agent.documents.current()['markdown'])

    def test_legacy_recovery_requires_explicit_resume(self):
        self.seed_legacy_stopped_checkpoint()
        with patch.object(self.agent, '_model') as model:
            with self.assertRaisesRegex(RuntimeError, 'explicitly resume'):
                asyncio.run(roles.generate(self.agent, {}, resume=False))
            model.assert_not_called()
        self.assertNotIn('writer_finalization', self.state['roles']['writer'][
            'no_progress']['effective_progress'])

    def test_legacy_recovery_rejects_unqualified_checkpoints_without_model_call(self):
        self.seed_legacy_stopped_checkpoint()
        original_state = deepcopy(self.state)
        draft_path = self.agent.directory / 'drafts/current.json'
        original_draft = draft_path.read_bytes()
        cases = ['finite', 'missing_baseline', 'nonempty_best', 'new_finding', 'offered',
                 'consumed', 'other_error', 'tool_errors', 'two_rounds'] + [
                 'truncation:' + status for status in
                 ('empty_output', 'repeated_truncation', 'exhausted', 'recovering', 'unaccounted')]
        for case in cases:
            with self.subTest(case=case):
                self.agent = SingleSessionAgent(self.agent.directory)
                draft_path.write_bytes(original_draft)
                state = deepcopy(original_state)
                writer = state['roles']['writer']
                progress = writer['no_progress']
                effective = progress['effective_progress']
                if case == 'finite':
                    state['stop_at'] = state['limits']['writer'] = 100
                elif case == 'missing_baseline':
                    effective.pop('draft_validation')
                elif case == 'nonempty_best':
                    effective['draft_validation']['best_findings'] = {'old defect': 1}
                elif case == 'new_finding':
                    current = draft.ensure(self.agent)
                    draft.edit(self.agent, current['draft_hash'], [
                        {'path': '/markdown', 'old': current['markdown'],
                         'new': current['markdown'] + '\n新增费用99元。'}])
                elif case in {'offered', 'consumed'}:
                    effective['writer_finalization'] = {
                        'opportunity_used': True, 'semantic_review': 'not_performed',
                        'draft_hash': draft.ensure(self.agent)['draft_hash'],
                        'request_started': case == 'consumed'}
                elif case == 'other_error':
                    writer['error']['message'] = 'role_no_progress: writer empty_output'
                elif case == 'tool_errors':
                    progress['last_errors'] = ['check_draft: failed']
                elif case == 'two_rounds':
                    progress['consecutive_stagnant_rounds'] = 2
                else:
                    writer['truncation_recovery'] = {'status': case.split(':')[1]}
                roles.private_json(self.agent.directory / 'roles/state.json', state)
                with patch.object(self.agent, '_model') as model:
                    with self.assertRaisesRegex(RuntimeError, 'role_no_progress'):
                        asyncio.run(roles.generate(self.agent, {}, resume=True))
                    model.assert_not_called()
                durable = json.loads((self.agent.directory / 'roles/state.json').read_text())
                finalization = durable['roles']['writer']['no_progress'][
                    'effective_progress'].get('writer_finalization')
                self.assertEqual(effective.get('writer_finalization'), finalization)
                self.assertIsNone(self.agent.documents.current())

    def test_legacy_final_request_failure_is_not_renewed_by_generate_resume(self):
        self.seed_legacy_stopped_checkpoint()
        requests = []
        async def fail():
            requests.append('failed')
            lease = self.agent.request_budget.reserve('llm', 'legacy-final-failure')
            self.agent.request_budget.settle(lease, status='failed')
            raise RuntimeError('offline legacy provider failed')
        with patch.object(self.agent, '_model', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'offline legacy provider failed'):
                asyncio.run(roles.generate(self.agent, {}, resume=True))
        self.agent = SingleSessionAgent(self.agent.directory)
        with patch.object(self.agent, '_model') as model:
            with self.assertRaisesRegex(RuntimeError, 'role_no_progress'):
                asyncio.run(roles.generate(self.agent, {}, resume=True))
            model.assert_not_called()
        self.assertEqual(['failed'], requests)
        durable = json.loads((self.agent.directory / 'roles/state.json').read_text())
        progress = durable['roles']['writer']['no_progress']
        self.assertEqual('stopped', progress['status'])
        self.assertTrue(progress['effective_progress']['writer_finalization']['request_started'])
        self.assertTrue(progress['effective_progress']['writer_finalization']['legacy_stopped_resume'])
        self.assertIsNone(self.agent.documents.current())


if __name__ == '__main__':
    unittest.main()
