"""Destination-neutral runtime: parallel needs/intent, evidence, delivery, revision.

Each run directory is immutable. Intermediate views and the durable physical-call
ledger survive a failed stage; no retry is hidden in this orchestrator.
"""
from __future__ import annotations

import asyncio
import copy
import html
import hashlib
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from .comparison_card_budget import DurableBudget, now, private_json
from .intent_questions_model import IntentQuestionsModel
from .intent_revision import IntentRevisionService
from .intent_revision_model import IntentRevisionModel
from .itinerary_assembly_model import AssemblyModel
from .itinerary_assembly_pipeline import assemble_case, reassembly_fixture, review_cases, run_case_revision
from .revision_plan import plan_sha256
from .runtime_planning import (bind_prices, execution_approval, feedback_card, generate_plan,
                               make_fixture, request_output, validate_intake, recover_recorded_collection)
from .store import TripStore


def local_preflight(directory):
    """Freeze the same native dependency identity, without making a request."""
    from .run_reliability import configured_run, require_run_preflight
    from .runtime_model_policy import model_policy
    if not os.environ.get('DEEPSEEK_API_KEY'):
        raise ValueError('DEEPSEEK_API_KEY missing; no product request sent')
    for key in ('GOOGLE_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY',
                'TRAVEL_AGENT_API_KEY', 'TRAVEL_AGENT_BASE_URL', 'TRAVEL_AGENT_MODEL',
                'TRAVEL_WRITER_API_KEY', 'TRAVEL_WRITER_BASE_URL'):
        os.environ.pop(key, None)
    os.environ.update(TRAVEL_AGENT_PROVIDER='deepseek', TRAVEL_WRITER_MODEL='deepseek-v4-pro',
                      TRAVEL_RUN_PYTHON=sys.executable)
    config = configured_run(('assembly', 'review', 'revision'))
    receipt = require_run_preflight(config=config, output_path=Path(directory)/'local-preflight.json')
    policies = {stage: model_policy(stage) for stage in
                ('assembly', 'review', 'revision', 'revision_patch', 'intent_questions', 'extraction', 'price_binding')}
    result = dict(passed=receipt['passed'], configuration=config, tiers=policies,
                  remote_connectivity_tested=False, product_calls=0, created_at=now())
    private_json(Path(directory)/'configuration.json', result)
    return result


def progressive_html(intake, *, stage, atoms=(), intent=None, baseline_plan=None):
    """Known dates/profile first; never manufacture a sightseeing route."""
    escape = lambda value: html.escape(str(value))
    start = date.fromisoformat(intake['start_date'])
    days = ''.join('<article><h2>Day '+str(n+1)+' · '+(start+timedelta(days=n)).isoformat()+
        '</h2><p>安排正在研究；景点、接驳与费用未核验，尚不可据此出发。</p></article>'
        for n in range(intake['expected_days']))
    if baseline_plan is not None:
        days = '<p>历史模型草案复用，仅作本次审订起点；本轮补采、报价及修订尚未完成。</p>' + ''.join(
            '<article><h2>'+escape(day['date'])+' · '+escape(day['title'])+'</h2><ul>'+''.join(
                '<li>'+escape(item['time'])+'–'+escape(item.get('end_time', ''))+' '+escape(item['title'])+'</li>'
                for item in day['items'])+'</ul></article>' for day in baseline_plan['days'])
    questions = '' if not intent else '<h2>会改变方案的选择</h2>' + ''.join(
        '<article><strong>'+escape(q['text'])+'</strong><p>'+escape(q['why_it_matters'])+
        '</p><p>'+' / '.join(escape(c['label']) for c in q['choices'])+'</p></article>'
        for q in intent.get('questions', []))
    evidence = ''.join('<li>'+escape(row['claim'])+
        (' · 当前参考／核验日 ' if row.get('grade') == 'S1' and row.get('verification_status') == 'verified'
         else ' · 待核验线索／采集日 ')+escape(row.get('checked_at', '未知'))+
        '，出行前复核 · <a href="'+escape(row['source_url'])+'">来源</a></li>' for row in atoms)
    return ('<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'+(
        '<title>'+escape(intake['destination']['city'])+' · 旅行工作稿</title>')+'''
<style>body{font:17px/1.7 system-ui;color:#20312c;background:#f6f5ef;max-width:920px;margin:40px auto;padding:0 24px}
article{background:white;padding:18px 24px;margin:16px 0;border-radius:16px}h1{font-size:36px}small{color:#53655e}a{color:#27684c}</style>
'''+'<small>正在规划 · '+escape(stage)+'</small><h1>'+escape(intake['destination']['city'])+
        '</h1><p>'+escape(intake['brief'])+'</p>'+questions+days+'<h2>已取得的参考证据</h2><ul>'+evidence+
        '</ul><p>这是一份进度骨架，不代表计划已核验或已预订。</p></html>')


async def collect_parallel(intent_call, supply_call):
    """Wait for both tasks even on failure so paid concurrent requests get settled."""
    results = await asyncio.gather(intent_call(), supply_call(), return_exceptions=True)
    failures = [result for result in results if isinstance(result, BaseException)]
    if failures:
        raise failures[0]
    return results


def merge_evidence(previous, update):
    """Monotone group publication; never replace earlier groups with the latest."""
    result = copy.deepcopy(update)
    previous = previous or {}
    for key in ('seeds', 'supply_seeds', 'sources', 'supply_evidence', 'original_sources', 'unknowns', 'conflicts'):
        rows, seen = [], {}
        for row in previous.get(key, []) + update.get(key, []):
            identity = (row.get('id') or row.get('seed_id')) if isinstance(row, dict) else None
            identity = identity or json.dumps(row, sort_keys=True, ensure_ascii=False)
            if identity in seen and seen[identity] != row:
                raise ValueError('Conflicting evidence identity: ' + key + ':' + identity)
            if identity not in seen:
                rows.append(copy.deepcopy(row))
                seen[identity] = row
        result[key] = rows
    return result


class RuntimeLoop:
    def __init__(self, intake, directory, cache_dir, resume_from=None, *, slim=False, baseline_fixture=None):
        self.intake = validate_intake(intake)
        self.directory, self.cache_dir = Path(directory).resolve(), Path(cache_dir).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clock = time.monotonic()
        self.slim = slim
        self.baseline = None
        if baseline_fixture is not None:
            from .itinerary_assembly_pipeline import _fixture
            self.baseline = _fixture(json.loads(Path(baseline_fixture).read_text()))
            dates = [day['date'] for day in self.baseline['plan']['days']]
            expected = [(date.fromisoformat(self.intake['start_date'])+timedelta(days=n)).isoformat()
                        for n in range(self.intake['expected_days'])]
            if (not slim or self.baseline['brief'] != self.intake['brief'] or dates != expected
                    or self.baseline['bundle']['destination'] != self.intake['destination']):
                raise ValueError('Explicit warm baseline must match this intake, dates and destination')
            private_json(self.directory/'baseline-reuse.json', dict(path=str(Path(baseline_fixture).resolve()),
                plan_sha256=plan_sha256(self.baseline['plan']), status='historical_model_draft_not_new_confirmation'))
        self.resume_from = Path(resume_from).resolve() if resume_from else None
        if self.resume_from and json.loads((self.resume_from/'intake.json').read_text()) != self.intake:
            raise ValueError('Resume requires the exact original intake')
        self.events, self.intent, self.supply = [], None, None
        self.id = self.intake['case_id']
        # Warm acquisition must not reuse an older run's source/atom identities.
        self.research_id = self.id+'-research' + ('-'+hashlib.sha256(
            str(self.directory).encode()).hexdigest()[:8] if slim else '')
        self.budget = DurableBudget(self.directory/'ledger', maximum_requests=1000000)
        self.state = dict(case_id=self.id, status='not_started', started_at=now(),
                          handbooks={}, known_limitations=[], budget_policy='no user hard cap')

    def event(self, stage, status, **details):
        row = dict(stage=stage, status=status, at=now(), elapsed_seconds=time.monotonic()-self.clock, **details)
        self.events.append(row)
        private_json(self.directory/'events.json', self.events)
        self.state.update(cursor=stage, elapsed_seconds=row['elapsed_seconds'])
        private_json(self.directory/'state.json', self.state)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        return row

    def progress(self, stage):
        path = self.directory/'progress'/f'{len(self.events):03d}-{stage}.html'
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(progressive_html(self.intake, stage=stage,
            atoms=(self.supply or {}).get('supply_seeds', []), intent=self.intent,
            baseline_plan=self.baseline['plan'] if self.baseline else None), encoding='utf-8')
        path.chmod(0o600)
        private_json(self.directory/'latest.json', dict(path=str(path), stage=stage, at=now()))
        return str(path)

    async def _stage(self, name, operation):
        self.event(name, 'started')
        try:
            with self.budget.stage(self.id, name):
                result = await operation()
            self.event(name, 'returned')
            return result
        except Exception as error:
            self.event(name, 'failed', error_type=type(error).__name__, error=str(error))
            raise

    async def _acquire(self, cached, pool):
        from .runtime_supply_acquisition import acquire_supply, NeedsPlan
        if self.slim and self.resume_from and (self.resume_from/'supply.json').exists():
            original = self.resume_from/'supply.json'
            value = json.loads(original.read_text())
            if (value.get('destination') != self.intake['destination']
                    or value.get('price_as_of') != date.today().isoformat()):
                raise ValueError('Saved supply requires same destination and current-day admission')
            private_json(self.directory/'supply-reuse.json', dict(source=str(original),
                sha256=hashlib.sha256(original.read_bytes()).hexdigest(), product_calls=0,
                authority='Resume completed acquisition; preserve quotes, grades and freshness unchanged'))
            self.supply = value
            return copy.deepcopy(value)
        planner = None
        if self.resume_from:
            original = self.resume_from/'ledger/model-artifacts'/f'{self.id}-research-plan-response.json'
            if original.exists():
                body = json.loads(json.loads(original.read_text())['body_utf8'])
                choice = body['choices'][0]
                if choice['finish_reason'] == 'tool_calls' and len(choice['message'].get('tool_calls', [])) == 1:
                    recorded = NeedsPlan.model_validate(json.loads(choice['message']['tool_calls'][0]['function']['arguments'])).model_dump()
                    class RecordedNeeds:
                        async def generate(self, seed, *, request_id):
                            return copy.deepcopy(recorded)
                    planner = RecordedNeeds()
                    private_json(self.directory/'needs-reuse.json', dict(original_response=str(original),
                        authority='runtime-loop removes artificial query-count limit; keeps explicit model query proposal',
                        product_calls=0, reused=recorded))
        self.supply = copy.deepcopy(cached)
        async def partial(bundle):
            self.supply = merge_evidence(self.supply, bundle)
            if self.supply.get('supply_seeds'):
                pool.put(self.intake['destination'], self.supply)
            path = self.progress('evidence')
            self.event('evidence', 'updated', atoms=len(self.supply['supply_seeds']), path=path)
        unmet = None
        if self.slim and self.baseline:
            unmet = dict(current_items=[dict(day=d['day'], items=[{k:i.get(k) for k in ('id','title','time')}
                for i in d['items']]) for d in self.baseline['plan']['days']],
                unpriced=[row for row in self.baseline['bundle'].get('price_rules', [])
                          if row.get('disposition') == 'unpriced'],
                known_limitations=['现有S2只作线索；缺经营者/主管机关可用原文与对应结构化报价',
                                   '日期在现有新鲜期外，仅当期参考、出行前复核；不重复搜历史天气背景'],
                existing_unknowns=cached.get('unknowns', []))
            private_json(self.directory/'unmet-input.json', unmet)
        return await acquire_supply(brief=self.intake['brief'], destination=self.intake['destination'],
            scope=dict(start_date=self.intake['start_date'], end_date=self.intake['end_date'],
                travelers=self.intake['travelers'], traveler_profile=self.intake['trip_context']),
            request_id=self.research_id, budget=self.budget, output_dir=self.directory/'acquisition',
            on_bundle=partial, concurrency=3, cached_bundle=cached, planner=planner, unmet_context=unmet)

    async def _intent(self):
        from .intent_questions import IntentQuestions, SYSTEM_PROMPT
        previous = self.resume_from/'intent.json' if self.resume_from else None
        if self.slim and self.baseline:
            value = IntentQuestions.model_validate(self.baseline['bundle']['intent_card']).model_dump()
            private_json(self.directory/'intent-reuse.json', dict(source='explicit_baseline_fixture', product_calls=0,
                user_answers_added=False))
        elif previous and previous.exists():
            value = IntentQuestions.model_validate(json.loads(previous.read_text())).model_dump()
            private_json(self.directory/'intent-reuse.json', dict(path=str(previous), product_calls=0))
        elif self.resume_from:
            value = recover_recorded_collection(self.resume_from/'ledger/model-artifacts'/f'{self.id}-intent-response.json',
                IntentQuestions, self.directory/'intent-format-recovery.json')
        else:
            try:
                value = await request_output(self.budget, self.id+'-intent',
                    dict(brief=self.intake['brief'], trip_context=self.intake['trip_context'],
                         previous_card=None, explicit_feedback=None, answer_id=None),
                    IntentQuestions, SYSTEM_PROMPT, 'intent_questions')
            except Exception as original:
                try:
                    value = recover_recorded_collection(self.budget.directory/'model-artifacts'/f'{self.id}-intent-response.json',
                        IntentQuestions, self.directory/'intent-format-recovery.json')
                except Exception:
                    raise original
        self.intent = value
        private_json(self.directory/'intent.json', value)
        self.event('intent', 'visible', path=self.progress('intent'))
        return value

    def _fixture_admission(self, fixture, model, folder):
        from .runtime_evidence_pool import preflight_runtime_fixture, admit_runtime_supply
        from .runtime_reference_delivery import build_reference_delivery
        fixture = copy.deepcopy(fixture)
        travel_dates = [day['date'] for day in fixture['plan']['days']]
        atom_admission = admit_runtime_supply(fixture['bundle'], as_of=date.today(), travel_dates=travel_dates)
        private_json(folder/'atom-admission.json', atom_admission)
        initial_approval = execution_approval(fixture)
        admission = preflight_runtime_fixture(fixture, initial_approval, model, folder/'before-reference', as_of=date.today())
        fixture['provenance']['runtime_reference_admission'] = build_reference_delivery(fixture, admission)
        approval = execution_approval(fixture)
        admission = preflight_runtime_fixture(fixture, approval, model, folder, as_of=date.today())
        return fixture, approval, admission

    async def _assemble(self, fixture, folder, suffix):
        from .runtime_reference_delivery import apply_reference_delivery
        model = AssemblyModel(self.budget, settings_override={'reasoning_effort': 'high'}, runtime_slim=self.slim)
        fixture, approval, admission = self._fixture_admission(fixture, model, folder)
        case = await asyncio.to_thread(assemble_case, fixture, approval, model,
            self.id+'-'+suffix, folder/'raw-case')
        return apply_reference_delivery(case, admission, folder/'delivery')

    async def _revise(self, case):
        from .runtime_revision import REVISION_REVIEW_PROMPT, RevisionReview
        folder = self.directory/'revision'
        folder.mkdir(exist_ok=True)
        feedback = '第二天太赶了'
        store = TripStore(folder/'isolated-trip.sqlite3')
        model = IntentRevisionModel(self.budget, runtime_slim=True) if self.slim else IntentRevisionModel(
            self.budget, settings_override={'reasoning_effort': 'high'})
        service = IntentRevisionService(store, model, runtime_slim=self.slim)
        trip, token = store.create(self.intake['brief'], '', [])
        store.commit_plan(trip['id'], 1, case['plan'], {})
        private_json(folder/'private-access.json', dict(trip_id=trip['id'], token=token))
        def semantic_model(*, request_id, payload, mode):
            return asyncio.run(request_output(self.budget, request_id, payload,
                RevisionReview, REVISION_REVIEW_PROMPT, 'review', input_stage='review' if self.slim else None))
        result = await asyncio.to_thread(run_case_revision, case, service, trip['id'], token, 1,
            self.id+'-revision', folder/'case', budget=self.budget,
            intent_card=feedback_card(feedback), selections=[dict(question_id='pace', choice_id='apply')],
            scope_day_numbers=[2], feedback=feedback, revision_review_model=semantic_model)
        applied = result['receipt']
        undone = service.undo(trip['id'], token, applied['trip']['revision'], self.id+'-undo',
                              applied['receipt']['revision_id'])
        restored = service.reapply(trip['id'], token, undone['trip']['revision'], self.id+'-reapply',
                                   applied['receipt']['revision_id'])
        proof = dict(undo_matches_original=plan_sha256(undone['trip']['plan']) == case['plan_sha256'],
            restore_matches_revised=plan_sha256(restored['trip']['plan']) == result['case']['plan_sha256'],
            undo=undone, restored=restored)
        private_json(folder/'undo-restore.json', proof)
        if not proof['undo_matches_original'] or not proof['restore_matches_revised']:
            raise ValueError('Actual undo/restore did not match saved versions')
        private_json(folder/'result.json', result)
        return result

    async def run(self, *, revise=False):
        from .runtime_evidence_pool import EvidencePool
        with (self.directory/'started').open('x') as marker:
            marker.write(now())
        self.state['status'] = 'running'
        private_json(self.directory/'intake.json', self.intake)
        self.state['handbooks']['skeleton'] = self.progress('skeleton')
        skeleton = self.event('skeleton', 'visible', path=self.state['handbooks']['skeleton'])
        self.state['skeleton_seconds'] = skeleton['elapsed_seconds']
        try:
            local_preflight(self.directory/'preflight')
            pool = EvidencePool(self.cache_dir)
            cached = pool.load(self.intake['destination'], date.today())
            private_json(self.directory/'cache-read.json', cached)
            self.event('cache', 'read', hits=cached['hits'], rejected=cached['rejected'])
            with self.budget.run(self.id, kind='runtime-loop-v1', case_id=self.id):
                self.intent, self.supply = await collect_parallel(
                    lambda: self._stage('intent', self._intent),
                    lambda: self._stage('acquisition', lambda: self._acquire(cached['bundle'], pool)))
                private_json(self.directory/'supply.json', self.supply)
                if not self.supply.get('supply_seeds'):
                    raise ValueError('evidence_insufficient: zero accepted atoms; skeleton retained')
                pool.put(self.intake['destination'], self.supply)
                if self.slim and self.baseline:
                    generated = dict(plan=copy.deepcopy(self.baseline['plan']),
                                     comparison_card=copy.deepcopy(self.baseline['bundle']['comparison_card']))
                    self.event('planning', 'reused', source='explicit_historical_model_draft', product_calls=0)
                else:
                    generated = await self._stage('planning', lambda: generate_plan(
                        self.intake, self.intent, self.supply, self.budget, self.id+'-plan'))
                private_json(self.directory/'generated-plan.json', generated)
                prices = await self._stage('price-binding', lambda: bind_prices(
                    self.intake, generated['plan'], self.supply, self.budget, self.id+'-prices'))
                fixture = make_fixture(self.intake, generated, self.intent, self.supply, prices)
                private_json(self.directory/'fixture.json', fixture)
                case = await self._stage('assembly', lambda: self._assemble(
                    fixture, self.directory/'assembly', 'assembly'))
                self.state['handbooks']['assembled'] = case['paths']['handbook_html']
                review = await self._stage('review', lambda: asyncio.to_thread(review_cases,
                    [case], AssemblyModel(self.budget, settings_override={'reasoning_effort': 'high'}, runtime_slim=self.slim), self.id+'-review', self.directory/'review', compact=True))
                private_json(self.directory/'review.json', review)
                self.state['full_handbook_seconds'] = time.monotonic()-self.clock
                self.state['review_outcomes'] = [row.get('review', {}).get('outcome') for row in review['cases']]
                self.state['handbooks']['reviewed'] = review['cases'][0]['paths']['handbook_html']
                if revise:
                    result = await self._stage('revision', lambda: self._revise(case))
                    self.state['revision_review'] = result.get('revision_review') or result['case'].get('revision_review')
                    if self.slim and (self.state['revision_review'] or {}).get('outcome') != 'pass':
                        raise ValueError('Revision semantic review did not pass; no automatic correction or reassembly request')
                    rebuilt = await self._stage('reassembly', lambda: self._assemble(
                        reassembly_fixture(result['case']), self.directory/'reassembly', 'reassembly'))
                    self.state['handbooks']['reassembled'] = rebuilt['paths']['handbook_html']
                self.state['status'] = 'delivered_pending_acceptance'
        except Exception as error:
            import traceback
            self.state.update(status='failed', error_type=type(error).__name__, error=str(error))
            private_json(self.directory/'exception.json', dict(type=type(error).__name__,
                message=str(error), traceback=traceback.format_exc()))
        finally:
            ledger = self.budget.snapshot()
            private_json(self.directory/'ledger.json', ledger)
            self.state.update(finished_at=now(), elapsed_seconds=time.monotonic()-self.clock,
                              calls=ledger['used'], cost='unknown')
            private_json(self.directory/'state.json', self.state)
            self.event('run', self.state['status'], calls=ledger['used'], cost='unknown')
        return copy.deepcopy(self.state)
