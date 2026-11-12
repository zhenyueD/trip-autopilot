"""A product-first journey using existing persistence, materials and plan checks.

The audited intake workflow remains available in runtime.py. This path does not
claim its source-alignment review passed: it publishes model hypotheses and a
proposal, not a verified/paid/booked trip.
"""
from __future__ import annotations

import copy
from functools import partial
import re
import time
from .phase1_budget_context import ContextThreadPoolExecutor as ThreadPoolExecutor

from . import atomic_prompts, core_prompts, model
from .core_commitments import (attach_commitments, attach_signature_schedule,
                               commitment_anchors, commitment_checks, merge_commitments)
from .geography import spatial_context
from .core_protocol import encode_references, proposal_from_model, display_supply_names, normalize_delta_response
from .runtime import StudioService, add_failed_usage, add_usage
from .store import StateError
from .evidence_context import planning_evidence
from .destination_discovery import validate_context
from .trip_graph import build_trip_graph, grounded_understanding, safe_assumptions, safe_questions
from .validation import PlanError, records, strings, text, validate_plan, validate_preview, substantive_change
from .delivery_truth import DeliveryTruthError, validate_delivery
from .pending_materials import active_materials, assigned_pending_ids, pending_after_assessment, revision_sources
from .full_revision_scope import (build_full_revision_scope, extend_full_revision_scope, focused_plan, research_scope,
                                  research_plan_scope, preparation_in_scope, prepare_full_response,
                                  restore_untouched_fields, validate_full_result)


def direction_view(raw, materials=None):
    days = raw.get('days')
    if type(days) is not int or not 1 <= days <= 14:
        raise PlanError('本原型支持1—14天，请明确范围；不会自动缩短你的旅行')
    preview = validate_preview(raw)
    preview['understanding'] = grounded_understanding(raw.get('understanding', []), materials or [])
    try:
        destinations, hints = validate_context(raw.get('destinations'), raw.get('place_hints'))
    except ValueError as error:
        raise PlanError(str(error)) from error
    queries = raw.get('research_queries', [])
    if not isinstance(queries, list):
        raise PlanError('research_queries必须是字符串数组，不能是字符串或对象')
    if len(queries) > 6:
        raise PlanError(f'research_queries最多6条，实际收到{len(queries)}条；请合并查询，不要删用户目的地')
    return {'title': text(raw.get('title'), '旅行构想'),
            'premise': text(raw.get('premise'), '旅行主张'), 'days': days,
            'assumptions': safe_assumptions(strings(raw.get('assumptions', []), '默认假设', 6)),
            'research_queries': strings(raw.get('research_queries', []), '研究查询', 6),
            'questions': safe_questions(strings(raw.get('questions', []), '硬条件', 3)),
            'destinations': destinations, 'place_hints': hints, **preview}


def explicit_commitment_updates(updates, materials):
    """Keep hard commitments only when quoted from direct user-authored text.

    Uploaded itineraries remain evidence-backed trip facts and planning input.
    Ordinary spreadsheet cells such as "lunch" must not become immutable,
    recurring service-level commitments merely because a model emitted them.
    """
    allowed = {row.get('id') for row in materials or []
               if isinstance(row, dict) and row.get('kind') in
               {'user_brief', 'user_text', 'user_feedback'}}
    result = []
    for row in updates if isinstance(updates, list) else []:
        source = row.get('source') if isinstance(row, dict) else None
        if isinstance(source, dict) and source.get('material_id') in allowed:
            result.append(row)
    return result


def actual_changes(previous, plan, request):
    """Explain actual changed surfaces; never report a model's invented diff."""
    if not previous:
        return []
    before = {d['day']: d for d in previous.get('days', [])}
    changes, preserved = [], []
    for day in plan['days']:
        target = preserved if before.get(day['day']) == day else changes
        target.append(f"第{day['day']}天 · {day['title']}")
    for day in before.values():
        if day['day'] > len(plan['days']):
            changes.append(f"移除第{day['day']}天 · {day['title']}")
    labels = {'flight': '航班', 'lodging': '住宿', 'transport': '交通', 'dining': '餐饮',
              'activities': '体验', 'packing': '行前准备', 'other': '其他准备'}
    for category, label in labels.items():
        old = [d for d in previous.get('decisions', []) if d['category'] == category]
        new = [d for d in plan['decisions'] if d['category'] == category]
        (preserved if old == new else changes).append(label)
    return [{'trigger': request or '本轮材料更新', 'changes': changes, 'preserved': preserved}]


def explicit_trip_years(payload):
    """Return the latest user-authored calendar years, never model guesses."""
    latest = payload.get('latest_request') or ''
    years = {int(value) for value in re.findall(r'(?<!\d)((?:19|20)\d{2})(?!\d)', latest)}
    if years:
        return years
    previous = payload.get('previous_plan') or {}
    prior = {int(day['date'][:4]) for day in previous.get('days', [])
             if isinstance(day, dict) and isinstance(day.get('date'), str)
             and re.fullmatch(r'\d{4}-\d{2}-\d{2}', day['date'])}
    if prior:
        return prior
    text_sources = (source.get('content', '') for source in payload.get('materials', [])
                    if source.get('kind') in ('user_brief', 'user_text', 'user_feedback'))
    return {int(value) for content in text_sources
            for value in re.findall(r'(?<!\d)((?:19|20)\d{2})(?!\d)', content)}


def bounded_planning_inputs(supplies, evidence, previous=None):
    """Keep the compose request useful without resending the full research ledger."""
    prior_ids = {item.get('supply_id') for day in (previous or {}).get('days', [])
                 for item in day.get('items', []) if item.get('supply_id')}
    prior_ids.update(id_ for row in (previous or {}).get('decisions', [])
                     for id_ in row.get('supply_ids', []))
    ordered = sorted(enumerate(supplies), key=lambda pair: (pair[1].get('id') not in prior_ids, pair[0]))
    selected = [row for _, row in ordered[:24]]
    selected_ids = {row.get('id') for row in selected}
    compact = planning_evidence(evidence)
    ranked = sorted(enumerate(compact), key=lambda pair: (
        not bool(selected_ids.intersection(pair[1].get('supply_ids', []))), pair[0]))
    return selected, [row for _, row in ranked[:36]]


def scoped_prompt_evidence(evidence, supplies):
    """Keep one best persisted source receipt per scoped supply.

    The full evidence ledger contains repeated reads and tool-execution records
    across revisions. A pure local edit needs the latest source receipt for each
    affected supply, not the entire append-only ledger.
    """
    fields = ('id', 'title', 'status', 'source_url', 'url', 'checked_at',
              'fetched_at', 'verified_fields', 'text', 'detail', 'warning',
              'message', 'body_sha256', 'truncated',
              'untrusted_external_content', 'method')
    chosen, order = {}, []
    rows = [row for row in evidence if isinstance(row, dict)
            and (row.get('source_url') or row.get('url'))]
    for supply in supplies:
        supply_id = supply.get('id')
        if not isinstance(supply_id, str):
            continue
        urls = {supply.get(key) for key in ('source_url', 'product_url') if supply.get(key)}
        candidates = [(index, row) for index, row in enumerate(rows)
                      if supply_id in row.get('supply_ids', [])
                      or (row.get('source_url') or row.get('url')) in urls]
        if not candidates:
            continue
        _, row = max(candidates, key=lambda pair: (
            pair[1].get('status') == 'read', bool(pair[1].get('text')),
            pair[1].get('fetched_at') or pair[1].get('checked_at') or '', pair[0]))
        key = row.get('id') or (row.get('source_url') or row.get('url'))
        if key not in chosen:
            compact = {field: copy.deepcopy(row[field]) for field in fields if field in row}
            compact['supply_ids'] = []
            chosen[key] = compact
            order.append(key)
        chosen[key]['supply_ids'].append(supply_id)
    for row in chosen.values():
        row['supply_ids'] = sorted(set(row['supply_ids']))
    return [chosen[key] for key in order]


def content_review_enabled(mode, previous_plan):
    """Production may review only revisions; tests/evaluation can review all candidates."""
    return mode is True or mode == 'revisions' and bool(previous_plan)


class _ScopedFallback(Exception):
    def __init__(self, cause, usage):
        super().__init__(str(cause))
        self.cause = cause
        self.usage = copy.deepcopy(usage)


def scoped_failure_details(error, stage=None):
    """Return a stable, auditable failure class without losing the original text."""
    if isinstance(error, model.ModelError):
        if error.usage.get('finish_reason') == 'length':
            code = 'model_truncation'
        elif error.format_error:
            code = 'model_format_error'
        elif error.transport_error:
            code = 'model_transport_error'
        else:
            code = 'model_error'
        stage = stage or 'model_response'
    else:
        code = 'plan_validation_error'
        stage = stage or 'scope_resolution'
    details = {'stage': stage, 'code': code, 'message': str(error)}
    if getattr(error, 'scope_violations', None):
        details['scope_violations'] = copy.deepcopy(error.scope_violations)
    return details


class CoreStudioService(StudioService):
    def __init__(self, *args, researcher=None, autonomous=True, content_review=False, route_builder=None,
                 place_localizer=None, preparation_builder=None, scoped_fallback='full', phase1_enabled=False, **kwargs):
        custom_caller = kwargs.get('caller') is not None
        kwargs.setdefault('caller', model.call_planner_json)
        super().__init__(*args, **kwargs)
        if researcher is None:
            from .research import ResearchService
            researcher = ResearchService(catalog=self.catalog, reader=self.link_reader)
        self.researcher = researcher
        self.autonomous = autonomous
        self.content_review = content_review
        self.route_builder = route_builder
        self.place_localizer = place_localizer
        self.preparation_builder = preparation_builder
        if scoped_fallback not in ('full', 'stop'):
            raise ValueError('scoped_fallback must be full or stop')
        self.scoped_fallback = scoped_fallback
        self.phase1_enabled = phase1_enabled
        if self.preparation_builder is None and not custom_caller:
            from .preparation import build_action_pack
            from .public_search import search_public_evidence
            self.preparation_builder = partial(build_action_pack,
                                               grounded_searcher=search_public_evidence)
        if place_localizer is None and not custom_caller:
            from .place_reading import localize_places
            self.place_localizer = lambda supplies: localize_places(supplies, self.caller)
        if self.phase1_enabled:
            from .phase1_runtime import Phase1Pool
            self.pool = Phase1Pool(self, self.pool)

    def _run(self, trip_id, revision, routing=None):
        if routing and routing.get('executed_pipeline') == 'writer_critic':
            from .writer_critic_runtime import run_revision
            run_revision(self, trip_id, revision, routing)
            return
        if routing and routing.get('executed_pipeline') == 'scoped':
            try:
                self._run_scoped(trip_id, revision, routing)
                return
            except _ScopedFallback as fallback:
                if self.scoped_fallback == 'stop':
                    try:
                        self.store.update_job(
                            trip_id, revision,
                            status=('needs_attention'
                                    if isinstance(fallback.cause, PlanError) else 'error'),
                            error=str(fallback.cause)[:500], metrics=fallback.usage,
                            event='局部诊断已停止；原方案保留，未执行全量回退')
                    except StateError:
                        pass
                    finally:
                        self.slots.release()
                    return
                routing = {**routing, 'executed_pipeline': 'full',
                           'fallback_reason': 'scoped_validation_failed'}
                self._run_full(trip_id, revision, routing, fallback.usage)
                return
        self._run_full(trip_id, revision, routing)

    def _run_full(self, trip_id, revision, routing=None, prior_usage=None):
        started = time.monotonic()
        usage = {'prompt_version': atomic_prompts.VERSION if self.autonomous else core_prompts.VERSION,
                 'workflow': 'atomic_journey' if self.autonomous else 'product_core',
                 'requested_pipeline': (routing or {}).get('requested_pipeline', 'initial'),
                 'executed_pipeline': 'full',
                 'fallback_reason': (routing or {}).get('fallback_reason'),
                 'affected_node_ids': [], 'skipped_stages': [],
                 'full_necessity': {'reason': '需先解析本轮影响范围；未确定边界前不编排。',
                                    'recompute_day_numbers': [], 'recompute_domains': []}}
        if prior_usage:
            add_usage(usage, prior_usage)
            usage['scoped_fallback_usage'] = {key: prior_usage.get(key, 0)
                for key in ('model_calls', 'input_tokens', 'output_tokens')}
            for key in ('scoped_failure', 'validation_rejections'):
                if key in prior_usage:
                    usage[key] = copy.deepcopy(prior_usage[key])
        prior_duration_ms = (prior_usage or {}).get('duration_ms', 0)
        accounting_started = started - prior_duration_ms / 1000
        reuse_research = usage['requested_pipeline'] == 'fact_correction'
        try:
            self.store.update_job(trip_id, revision, job_kind='journey')
            trip = self._initialize_materials(self.store.internal_get(trip_id), revision)
            scope = build_full_revision_scope(trip.get('plan'), routing,
                                             (prior_usage or {}).get('resolved_impact'))
            usage['full_revision_scope'] = scope
            usage['affected_node_ids'] = scope.get('affected_node_ids', [])
            usage['full_necessity'] = {'reason': scope['reason'],
                'recompute_day_numbers': scope['day_numbers'], 'recompute_domains': scope['domains']}
            if scope['kind'] == 'focused_revision':
                self._run_focused_full(trip, revision, routing, scope, usage,
                                       prior_duration_ms, started)
                return
            trip, payload, direction, research = self._full_planning_inputs(
                trip, revision, usage, reuse_research)
            if reuse_research:
                plan = self._compose(payload, research['supplies'], trip, usage)
                plan['understanding'] = copy.deepcopy(direction['understanding'])
                plan['core_notes'] = direction['assumptions']
                plan['decision_links'] = actual_changes(trip.get('plan'), plan, payload['latest_request'])
                self._event(trip_id, revision, '每日体验已形成，正在补齐剩余地点图片与出发准备',
                            draft_plan=plan if not trip.get('plan') else None)
                all_days = [day['day'] for day in plan['days']]
                self._enrich_scoped_routes(plan, trip.get('plan') or {}, research['supplies'],
                    {'route_days': all_days, 'day_numbers': all_days}, usage)
                usage['media_enriched_count'] = 0
                early_images = 0
            else:
                from .progressive_discovery import CandidatePresentation
                with CandidatePresentation(self, trip_id, revision, research['supplies'], usage) as presentation:
                    plan = self._compose(payload, research['supplies'], trip, usage)
                    plan['understanding'] = copy.deepcopy(direction['understanding'])
                    plan['core_notes'] = direction['assumptions']
                    plan['decision_links'] = actual_changes(trip.get('plan'), plan, payload['latest_request'])
                    self._event(trip_id, revision, '每日体验已形成，正在补齐剩余地点图片与出发准备',
                                draft_plan=plan if not trip.get('plan') else None)
                before_images = sum(bool(s.get('image_url')) for s in plan['supplies'])
                presentation.merge_plan(plan)
                early_images = sum(bool(s.get('image_url')) for s in plan['supplies']) - before_images
                self._enrich_plan(plan, research['supplies'], usage)
            self._prepare_plan(plan, trip, revision, usage, research['evidence'])
            if not trip.get('plan') and payload.get('detail_context'):
                from .detail_synthesis import validate_detail_actions
                # Compose already enforced the new actionable shape. The
                # legacy preparation builder has its own instructions/evidence
                # contract; validate explicit references here without imposing
                # a new schema on independently constructed legacy actions.
                plan = validate_detail_actions(plan, {}, payload['detail_context'],
                                               require_executable=False)
            if reuse_research and 'preparation_pack' not in plan:
                all_categories = [row.get('category') for row in plan.get('preparation', [])]
                self._update_scoped_preparation(plan, trip.get('plan') or {}, {
                    'preparation_action_ids': [], 'preparation_categories': all_categories})
            trip_graph = build_trip_graph(payload['materials'], direction, plan)
            usage['media_enriched_count'] += early_images
            usage['duration_ms'] = prior_duration_ms + round((time.monotonic() - started) * 1000)
            self.store.commit_plan(trip_id, revision, plan, usage,
                                   preview={'understanding': direction['understanding'],
                                            'experience_angles': direction['experience_angles']},
                                   research=research['stats'], trip_graph=trip_graph)
        except (model.ModelError, PlanError, StateError, ValueError, KeyError, TypeError) as error:
            self._fail(trip_id, revision, error, usage, accounting_started)
        except Exception:
            self._fail(trip_id, revision, model.ModelError('本轮没有完成，已有构想和方案保留，可以继续'), usage,
                       accounting_started)
        finally:
            self.slots.release()

    def _full_planning_inputs(self, trip, revision, usage, reuse_research):
        from .initial_checkpoints import restore_stage, save_stage
        trip_id = trip['id']
        restored = restore_stage(trip, self.autonomous)
        if restored is not None:
            # The same request reuses validated direction/research before any
            # model call. Regenerating either would invalidate all saved days.
            usage.update(copy.deepcopy(trip['revision_checkpoint'].get('usage', {})))
            usage['initial_stage_resumed'] = True
            usage['skipped_stages'] = ['read_images', 'read_links', 'direction', 'organize']
            payload, research = restored['payload'], restored['research']
            direction = payload['direction']
            self._event(trip_id, revision, '已恢复旅行方向与来源，继续尚未完成的逐日草稿',
                        core_direction=direction, source_evidence=research['evidence'])
            return trip, payload, direction, research
        usage['direction_policy'] = 'initial' if not trip.get('plan') else 'global_change'
        if reuse_research:
            usage['skipped_stages'] = ['read_images', 'read_links', 'organize',
                                       'candidate_presentation', 'image_enrichment']
        else:
            trip = self._read_images(trip, revision, usage)
            trip = self._read_links(trip, revision)
        payload = self._core_context(trip)
        self._event(trip_id, revision, '先替你想好这趟旅行的玩法与取舍')
        direction = self._direction(payload, trip, revision, usage)
        if not trip.get('plan'):
            usage['full_necessity']['recompute_day_numbers'] = list(range(1, direction['days'] + 1))
        self._event(trip_id, revision, '已从材料恢复旅行事实，正在寻找合适的体验、酒店与餐饮',
                    core_direction=direction, preview={'understanding': direction['understanding'],
                    'experience_angles': direction['experience_angles']},
                    trip_graph=build_trip_graph(payload['materials'], direction), intake=None)
        if reuse_research:
            research = {'supplies': copy.deepcopy((trip.get('plan') or {}).get('supplies', [])),
                        'evidence': copy.deepcopy(trip.get('source_evidence') or []),
                        'work': self._scoped_work(trip, trip.get('plan') or {}),
                        'stats': copy.deepcopy(trip.get('research') or {})}
            usage['research'] = {'reused': True, 'candidate_count': len(research['supplies'])}
        else:
            research = self._organize(direction, payload, trip, revision, usage)
            usage['research'] = research['stats']
        resource_note = ('候选资源已整理，正在组织路线、住宿、餐饮和行前准备' if research['supplies']
                         else '本轮公开来源未返回地点；正在保留可用构想并标明资料缺口')
        self._event(trip_id, revision, resource_note, source_evidence=research['evidence'])
        planning_supplies, planning_sources = bounded_planning_inputs(
            research['supplies'], research['evidence'], trip.get('plan'))
        usage['planning_context'] = {'supply_count': len(planning_supplies),
                                     'evidence_count': len(planning_sources)}
        payload.update(direction=direction, catalog=self._catalog_context(planning_supplies),
                       source_evidence=planning_sources, geography=spatial_context(research['supplies']))
        if self.autonomous:
            payload['work'] = research['work']
        if not trip.get('plan'):
            trip = self.store.internal_get(trip_id)
            save_stage(self, trip, payload, research, usage)
        return trip, payload, direction, research

    def _run_focused_full(self, trip, revision, routing, scope, usage, prior_ms, started):
        """Keep full structural permissions inside a persisted dependency closure."""
        previous = trip['plan']
        pending = [s for s in trip.get('material_sources', []) if s.get('status') == 'pending']
        if any(s.get('kind') == 'image' for s in pending):
            trip = self._read_images(trip, revision, usage)
        else:
            usage['skipped_stages'].append('read_images')
        if any(s.get('kind') == 'web_link' for s in pending):
            trip = self._read_links(trip, revision)
        else:
            usage['skipped_stages'].append('read_links')
        payload = self._core_context(trip)
        merge_base = copy.deepcopy(payload['previous_plan'])
        new_materials = revision_sources(trip, routing.get('feedback', ''))
        scope = extend_full_revision_scope(previous, routing, scope, materials=new_materials)
        pool = pending_after_assessment(trip.get('pending_materials') or [], new_materials, scope)
        self.store.update_job(trip['id'], revision, pending_materials=pool)
        trip = {**trip, 'pending_materials': pool}
        pending_ids = {row['source_id'] for row in pool}
        new_materials = [row for row in new_materials if row['id'] not in pending_ids]
        # Unassigned content cannot influence constraints or discovery while it
        # awaits the user's date/topic assignment. Keep its original receipt.
        payload['materials'] = new_materials
        payload['current_source_ids'] = [row['id'] for row in new_materials]
        usage['pending_material_count'] = len(pool)
        if pool and not routing.get('feedback', '').strip() and not new_materials:
            # A material-only upload with no resolved purpose is a saved
            # receipt, not authorization to replan every day of the trip.
            usage.update(executed_pipeline='materials_pending',
                         duration_ms=prior_ms + round((time.monotonic() - started) * 1000))
            self.store.update_job(trip['id'], revision,
                status=routing.get('previous_status', 'ready'),
                error=routing.get('previous_error'), metrics=usage,
                event='材料已保存，等待指派日期和事项；现有方案保持不变')
            return
        direction = self._scoped_direction(trip, previous)
        constraint_update = (routing.get('requested_pipeline') == 'fact_correction'
                             or re.search(r'每天|每日|不再要求', routing.get('feedback', ''))
                             or any(s.get('kind') not in ('user_feedback',) for s in new_materials))
        usage['direction_policy'] = 'constraint_update' if constraint_update else 'reuse'
        if constraint_update:
            extracted = self._direction({**payload, 'direction_policy': 'constraints_only',
                'preserved_direction': direction}, trip, revision, usage)
            for field in ('understanding', 'questions', 'user_commitments'):
                direction[field] = extracted[field]
            scope = extend_full_revision_scope(previous, routing, scope,
                                                commitments=direction['user_commitments'])
        else:
            usage['skipped_stages'].append('direction')
        # Never replay old discovery queries or tool actions merely because
        # the existing taste/direction is being reused for a local revision.
        direction.update(research_queries=[], work_plan={'goal': '补齐本轮范围内的实际执行缺口', 'actions': []})
        usage['full_revision_scope'] = scope
        usage['affected_node_ids'] = scope['affected_node_ids']
        usage['full_necessity'] = {'reason': scope['reason'],
            'recompute_day_numbers': scope['day_numbers'], 'recompute_domains': scope['domains']}
        focus = research_scope(previous, scope)
        public_scope = research_plan_scope(previous, scope)
        local = focused_plan(previous, public_scope)
        payload.update(previous_plan=focused_plan(merge_base, scope),
                       materials=new_materials, current_source_ids=[s['id'] for s in new_materials],
                       direction=direction, research_scope=focus,
                       full_revision_scope=scope,
                       neighbor_context=self._scoped_neighbors(previous, scope['day_numbers']),
                       commitment_anchors=commitment_anchors(direction.get('user_commitments', []), direction['days']))
        payload['read_only_preparation'] = [row for row in previous.get('preparation', [])
                                           if not preparation_in_scope(row, scope)]
        local_trip = {**trip, 'plan': local,
                      'source_evidence': self._scoped_evidence(trip.get('source_evidence', []), local.get('supplies', []))}
        self._event(trip['id'], revision, '保留已有旅行方向，只处理本轮影响范围',
                    metrics=usage)
        if routing.get('requested_pipeline') == 'fact_correction':
            research = {'supplies': local.get('supplies', []), 'evidence': local_trip['source_evidence'],
                        'work': self._scoped_work(trip, previous), 'stats': {'reused': True}}
            usage['skipped_stages'].append('organize')
        else:
            research_payload = {**payload, 'previous_plan': focused_plan(merge_base, public_scope)}
            research = self._organize(direction, research_payload, local_trip, revision, usage,
                                      retained_evidence=trip.get('source_evidence', []))
        usage['research'] = research['stats']
        all_supplies = {row['id']: row for row in previous.get('supplies', [])}
        all_supplies.update({row['id']: row for row in research['supplies']})
        supplies = list(all_supplies.values())
        work = research.get('work') or self._scoped_work(trip, previous)
        payload.update(catalog=self._catalog_context(research['supplies']),
                       source_evidence=research['evidence'], geography=spatial_context(research['supplies']),
                       work=work)
        old_work = self._scoped_work(trip, previous)
        delivery_work = {**work, 'tasks': old_work['tasks'] + [r for r in work.get('tasks', [])
                                                            if r not in old_work['tasks']],
                         'open_gaps': old_work['open_gaps'] + [r for r in work.get('open_gaps', [])
                                                             if r not in old_work['open_gaps']]}
        plan = self._compose(payload, supplies, trip, usage, merge_base_plan=merge_base,
                             full_scope=scope, delivery_work=delivery_work)
        plan['core_notes'] = copy.deepcopy(direction['assumptions'])
        plan['decision_links'] = actual_changes(previous, plan, payload['latest_request'])
        self._enrich_scoped_routes(plan, previous, research['supplies'], scope, usage)
        # Preserve the exact existing route records outside the closure,
        # including absence of a record; do not manufacture new default rows.
        plan['routes'] = [copy.deepcopy(r) for r in previous.get('routes', [])
                          if r.get('day') not in scope['day_numbers']] + [
                              r for r in plan.get('routes', []) if r.get('day') in scope['day_numbers']]
        plan['routes'].sort(key=lambda row: row.get('day', 0))
        self._prepare_focused(plan, trip, revision, usage, research['evidence'], scope, focus)
        usage['skipped_stages'] += ['candidate_presentation', 'image_enrichment', 'taste_making']
        usage['reused_stages'] = ['impact_scope', 'direction_taste', 'persisted_materials', 'persisted_evidence']
        usage['media_enriched_count'] = 0
        usage['duration_ms'] = prior_ms + round((time.monotonic() - started) * 1000)
        if constraint_update:
            # Failed candidates must not replace the persisted old direction.
            # Pure reuse keeps it byte-for-byte, including old queries which
            # are deliberately not executed by this revision's local payload.
            self.store.update_job(trip['id'], revision, core_direction=direction)
        self.store.commit_plan(trip['id'], revision, plan, usage, research=research['stats'],
                               trip_graph=build_trip_graph(active_materials(trip), direction, plan))

    def _prepare_focused(self, plan, trip, revision, usage, sources, scope, focus):
        # Composition has already passed the original write-scope gate. New
        # items on those authorized days now own preparation too; they must not
        # widen the earlier model permissions or introduce other days.
        scope = {**scope, 'item_ids': sorted(set(scope['item_ids']) | {
            item['id'] for day in plan['days'] if day['day'] in scope['day_numbers']
            for item in day['items']})}
        local = focused_plan(plan, scope)
        # Preserve original public anchors alongside selected new candidates.
        # Preparation reuses their exact registered query IDs to independently
        # verify candidate admission against the research execution receipts.
        keep_ids = set(focus['supply_ids']) | {s['id'] for s in local.get('supplies', [])}
        local['supplies'] = [copy.deepcopy(s) for s in plan.get('supplies', []) if s['id'] in keep_ids]
        old_local = focused_plan(trip['plan'], scope)
        local_trip = {**trip, 'plan': old_local}
        self._prepare_plan(local, local_trip, revision, usage, sources, research_scope=focus)
        old = (trip['plan'].get('preparation_pack') or {})
        if 'preparation_pack' not in local:
            self._update_scoped_preparation(plan, trip['plan'], scope)
            if local.get('preparation_error'):
                plan['preparation_error'] = local['preparation_error']
            return
        fresh = local['preparation_pack']
        incoming = fresh.get('actions', [])
        if any(not preparation_in_scope(row, scope) for row in incoming):
            raise PlanError('准备行动超出本轮全量修订范围，原方案保留')
        retained = [copy.deepcopy(row) for row in old.get('actions', []) if not preparation_in_scope(row, scope)]
        if {r.get('id') for r in retained}.intersection(r.get('id') for r in incoming):
            raise PlanError('准备行动覆盖了范围外的既有事项，原方案保留')
        fresh['actions'] = retained + incoming
        known = {r['id']: r for r in old.get('sources', []) if r.get('id')}
        known.update({r['id']: r for r in fresh.get('sources', []) if r.get('id')})
        fresh['sources'] = list(known.values())
        if old.get('status') == 'partial' and retained:
            fresh['status'] = 'partial'
        plan['preparation_pack'] = fresh

    def _direction(self, payload, trip, revision, usage):
        """Validate direction before research; allow only two protocol repairs."""
        from .direction_repair import invalid_activity_kinds, repair_payload, retain_commitment_updates
        from .direction_schema import DIRECTION_SCHEMA, direction_messages, query_diagnostics

        prompt = atomic_prompts.direction_messages if self.autonomous else core_prompts.direction_messages
        request, required = payload, {}
        preview = None
        for attempt in range(3):
            # Keep the existing intent budget; repairs never restart research.
            usage['direction_repair_count'] = attempt
            raw, met = self.caller(direction_messages(prompt(request)), max_tokens=5000,
                                   reasoning_effort='none', response_schema=DIRECTION_SCHEMA)
            add_usage(usage, met)
            try:
                if not isinstance(raw, dict):
                    raise PlanError('旅行方向必须为JSON对象')
                all_updates = records(raw.get('commitment_updates', []), '每日要求更新', 8)
                updates = explicit_commitment_updates(all_updates, payload['materials'])
                retain_commitment_updates(updates, required)
                direction = direction_view(raw, payload['materials'])
                preview = {'understanding': direction['understanding'],
                           'experience_angles': direction['experience_angles']}
                if invalid_activity_kinds({'commitment_updates': updates}):
                    raise PlanError('每日要求的活动类型不正确')
                commitments = merge_commitments(updates,
                    (trip.get('plan') or {}).get('user_commitments', []),
                    payload['materials'], payload['current_source_ids'])
                anchors = commitment_anchors(commitments, direction['days'])
            except (PlanError, TypeError, ValueError, KeyError) as error:
                # Only bounded field diagnostics persist; the rejected object
                # is used for the in-memory repair prompt, never usage/events.
                rejection = {'attempt': attempt + 1, 'code': 'direction_protocol_error',
                             'invalid_activity_kinds': invalid_activity_kinds(raw),
                             'query_shape': query_diagnostics(raw)}
                usage.setdefault('direction_validation_rejections', []).append(rejection)
                usage['direction_repair_exhausted'] = attempt == 2
                fields = {'metrics': usage}
                if preview and not trip.get('plan'):
                    fields['preview'] = preview
                self._event(trip['id'], revision,
                    ('旅行方向修复未完成；已有结果保留，可调整或重试' if attempt == 2 else
                     f'旅行方向结构需要修正，正在进行第{attempt + 1}/2次修复；已有结果保留'),
                    **fields)
                if attempt == 2:
                    reason = str(error) if isinstance(error, PlanError) else '方向字段结构不正确'
                    raise PlanError(f'旅行方向经过2次修复仍未通过：{reason}；'
                                    '已有结果保留，可调整要求或重试') from error
                request = repair_payload(payload, raw, error)
                continue
            if self.autonomous:
                direction['work_plan'] = raw.get('work_plan', {})
                direction['learning_updates'] = raw.get('learning_updates', [])
            if len(all_updates) > len(updates):
                usage['ignored_non_user_commitment_updates'] = len(all_updates) - len(updates)
            direction['user_commitments'] = commitments
            payload['commitment_anchors'] = anchors
            return direction

    def _run_scoped(self, trip_id, revision, routing):
        """Apply one resolver-bounded revision without material or research I/O."""
        started = time.monotonic()
        usage = {
            'prompt_version': atomic_prompts.VERSION if self.autonomous else core_prompts.VERSION,
            'workflow': 'atomic_journey' if self.autonomous else 'product_core',
            'requested_pipeline': routing['requested_pipeline'],
            'executed_pipeline': 'scoped',
            'fallback_reason': None,
            'affected_node_ids': [],
            'skipped_stages': ['read_images', 'read_links', 'organize',
                               'image_enrichment', 'full_preparation'],
        }
        try:
            self._execute_scoped(trip_id, revision, routing, usage, started)
        except DeliveryTruthError as error:
            # Truth has its own single repair. A full fallback must not reset
            # that budget or turn an unfulfilled request into a fresh attempt.
            self._fail(trip_id, revision, error, usage, started)
            self.slots.release()
        except (model.ModelError, PlanError) as error:
            add_failed_usage(usage, error)
            usage.setdefault('scoped_failure', scoped_failure_details(error))
            usage['duration_ms'] = round((time.monotonic() - started) * 1000)
            raise _ScopedFallback(error, usage) from error
        except Exception:
            try:
                self._fail(trip_id, revision,
                           model.ModelError('局部调整未完成，原方案已保留'), usage, started)
            finally:
                self.slots.release()
        else:
            self.slots.release()

    def _execute_scoped(self, trip_id, revision, routing, usage, started):
        from .scoped_replan import resolve_plan_impact

        trip = self.store.internal_get(trip_id)
        previous = trip.get('plan')
        if not previous:
            raise PlanError('局部修改需要已有方案')
        impact = resolve_plan_impact(previous, routing)
        usage['resolved_impact'] = copy.deepcopy(impact)
        usage['affected_node_ids'] = impact['affected_node_ids']
        from .scoped_time_shift import plan_time_shift
        try:
            shifted = plan_time_shift(previous, impact, routing['feedback'],
                                      trip.get('locked_item_ids', []))
            if shifted is not None:
                self._execute_time_shift(trip, revision, routing, impact, shifted, usage, started)
                return
        except PlanError as error:
            usage['scoped_strategy'] = 'deterministic_time_shift'
            usage['scoped_failure'] = scoped_failure_details(error, 'time_shift')
            self._fail(trip_id, revision, error, usage, started)
            return
        if not impact['day_numbers'] or not impact['item_ids']:
            raise PlanError('无法从现有方案确定安全的局部范围')
        unsafe_preparation = (set(impact['preparation_categories'])
                              - set(impact['replaceable_preparation_categories']))
        if unsafe_preparation:
            raise PlanError('同类准备项跨越多个范围，需要保守升级全量重算')
        self.store.update_job(trip_id, revision, job_kind='journey')
        context = self._core_context(trip)
        context['current_source_ids'] = [row['id'] for row in revision_sources(trip)]
        merge_base = context['previous_plan']
        direction = self._scoped_direction(trip, merge_base)
        context['previous_plan'] = self._scoped_plan_view(merge_base, impact)
        context['neighbor_context'] = self._scoped_neighbors(merge_base, impact['day_numbers'])
        context['route_context'] = [copy.deepcopy(route) for route in previous.get('routes', [])
                                    if route.get('day') in set(impact['route_days'])]
        context['preparation_context'] = [copy.deepcopy(action)
            for action in (previous.get('preparation_pack') or {}).get('actions', [])
            if action.get('id') in set(impact['preparation_action_ids'])]
        context['commitment_anchors'] = commitment_anchors(
            direction.get('user_commitments', []), direction['days'])
        supplies = copy.deepcopy(previous.get('supplies', []))
        relevant_ids = self._scoped_supply_ids(context, impact)
        planning_supplies = [row for row in supplies if row.get('id') in relevant_ids]
        planning_evidence = self._scoped_evidence(
            trip.get('source_evidence', []), planning_supplies)
        full_work = self._scoped_work(trip, previous)
        context['materials'] = [row for row in context['materials']
                                if row.get('id') in set(context['current_source_ids'])]
        context.update(direction=direction, catalog=self._catalog_context(planning_supplies),
                       source_evidence=planning_evidence,
                       geography=spatial_context(planning_supplies),
                       work={'goal': full_work['goal'], 'tasks': [], 'open_gaps': [],
                             'experience_brief': full_work['experience_brief']})
        self._event(trip_id, revision, '已确定影响范围，正在局部调整行程')
        plan = self._compose(context, supplies, trip, usage,
                             merge_base_plan=merge_base, allowed_impact=impact,
                             delivery_work=full_work)
        plan['understanding'] = copy.deepcopy(direction['understanding'])
        plan['core_notes'] = copy.deepcopy(direction['assumptions'])
        plan['decision_links'] = actual_changes(previous, plan, context['latest_request'])
        self._enrich_scoped_routes(plan, previous, supplies, impact, usage)
        self._update_scoped_preparation(plan, previous, impact)
        usage['media_enriched_count'] = 0
        usage['duration_ms'] = round((time.monotonic() - started) * 1000)
        graph = build_trip_graph(active_materials(trip), direction, plan)
        self.store.commit_plan(trip_id, revision, plan, usage, trip_graph=graph)

    def _execute_time_shift(self, trip, revision, routing, impact, shifted, usage, started):
        from .journey_state import plan_changes
        from .scoped_time_copy import rewrite_time_copy

        previous = trip['plan']
        usage.update(scoped_strategy='deterministic_time_shift', structure_model_calls=0,
                     shift_minutes=shifted['shift_minutes'],
                     shifted_item_ids=[row['item_id'] for row in shifted['changes']])
        usage['skipped_stages'] += ['direction', 'compose_delta', 'semantic_review', 'route_research']
        self._event(trip['id'], revision, '时间已按你的要求调整，正在同步相关说明')
        plan, copy_usage = rewrite_time_copy(previous, shifted['plan'], shifted['changes'],
                                              shifted['summary'], self.caller)
        add_usage(usage, copy_usage)
        usage.update({key: value for key, value in copy_usage.items()
                      if key.startswith('time_copy_')})
        # These are derived views, never a second model-owned plan.
        attach_signature_schedule(plan, plan)
        plan['decision_links'] = actual_changes(previous, plan, routing['feedback'])
        if isinstance(plan.get('journey'), dict):
            plan['journey']['changes'] = plan_changes(previous, plan, routing['feedback'])
        self._update_scoped_preparation(plan, previous, impact)
        if copy_usage.get('time_copy_limitations'):
            plan['change_summary'].append('部分说明含固定时间条件，原条件已保留；新时段仍需核对是否适用。')
        # Do not inherit the previous revision's delivery report or global
        # success prose. This summary is owned by the verified shift program.
        shift_summary = list(plan['change_summary'])
        report = validate_delivery(previous, plan, routing['feedback'])
        report['goal_verification'] = 'deterministic_time_shift'
        report['summary_source'] = 'deterministic_time_shift'
        plan['change_summary'] = shift_summary
        plan['summary'] = '本轮时间调整：' + ' '.join(shift_summary)
        plan['reactions'] = []
        usage['delivery_truth'] = report
        usage['duration_ms'] = round((time.monotonic() - started) * 1000)
        usage['media_enriched_count'] = 0
        direction = self._scoped_direction(trip, plan)
        graph = build_trip_graph(active_materials(trip), direction, plan)
        self.store.commit_plan(trip['id'], revision, plan, usage, trip_graph=graph)

    @staticmethod
    def _scoped_supply_ids(context, impact):
        item_ids = set(impact['item_ids'])
        ids = {item.get('supply_id') for day in context['previous_plan'].get('days', [])
               for item in day.get('items', []) if item.get('id') in item_ids}
        ids.update(row.get('item', {}).get('supply_id') for row in context['neighbor_context'])
        for field in ('decisions', 'preparation'):
            for row in context['previous_plan'].get(field, []):
                ids.update(row.get('supply_ids', []))
        return {id_ for id_ in ids if isinstance(id_, str)}

    @staticmethod
    def _scoped_evidence(evidence, supplies):
        return scoped_prompt_evidence(evidence, supplies)

    @staticmethod
    def _scoped_plan_view(plan, impact):
        """Return only affected model context; this is never the merge base."""
        view = copy.deepcopy(plan)
        days = set(impact['day_numbers'])
        decisions = set(impact['decision_categories'])
        preparation = set(impact['preparation_categories'])
        view['days'] = [day for day in view.get('days', []) if day.get('day') in days]
        read_only_neighbors = {'lodging'} if decisions.intersection({'transport', 'lodging'}) else set()
        view['decisions'] = [row for row in view.get('decisions', [])
                             if row.get('category') in decisions | read_only_neighbors]
        view['preparation'] = [row for row in view.get('preparation', [])
                               if row.get('category') in preparation]
        journey = view.get('journey') or {}
        view['journey'] = {'artifacts': [artifact for artifact in journey.get('artifacts', [])
            if set(artifact.get('related_item_ids', [])).intersection(impact['item_ids'])]}
        return view

    @staticmethod
    def _scoped_neighbors(plan, affected_days):
        """Expose boundary connections as read-only context, never editable scope."""
        days = {day.get('day'): day for day in plan.get('days', [])}
        result = []
        for number in affected_days:
            previous, following = days.get(number - 1), days.get(number + 1)
            if previous and previous.get('items'):
                result.append({'day': number - 1, 'edge': 'previous',
                               'item': copy.deepcopy(previous['items'][-1]),
                               'lodging': copy.deepcopy(previous.get('lodging'))})
            if following and following.get('items'):
                result.append({'day': number + 1, 'edge': 'next',
                               'item': copy.deepcopy(following['items'][0]),
                               'lodging': copy.deepcopy(following.get('lodging'))})
        return result

    @staticmethod
    def _scoped_direction(trip, plan):
        direction = copy.deepcopy(trip.get('core_direction') or {})
        direction.setdefault('title', plan.get('title', '旅行构想'))
        direction.setdefault('premise', plan.get('summary', '保留原行程的核心取舍'))
        direction['days'] = len(plan.get('days', []))
        direction.setdefault('assumptions', copy.deepcopy(plan.get('core_notes', [])))
        direction.setdefault('understanding', copy.deepcopy(plan.get('understanding', [])))
        direction.setdefault('experience_angles', [])
        direction.setdefault('questions', copy.deepcopy(plan.get('questions', [])))
        direction.setdefault('destinations', [])
        direction.setdefault('place_hints', [])
        direction.setdefault('user_commitments', copy.deepcopy(plan.get('user_commitments', [])))
        direction.setdefault('work_plan', {})
        direction.setdefault('learning_updates', [])
        return direction

    @staticmethod
    def _scoped_work(trip, plan):
        prior = plan.get('journey') or {}
        stored = (((trip.get('revision_base') or {}).get('material_state') or {})
                  .get('agent_work') or {})
        tasks = [copy.deepcopy(task) for task in prior.get('tasks', [])
                 if task.get('kind') not in ('preparation', 'digital_material', 'capability_gap')]
        return {
            'goal': prior.get('goal') or stored.get('goal', ''),
            'tasks': tasks,
            'open_gaps': copy.deepcopy(stored.get('open_gaps', [])),
            'experience_brief': copy.deepcopy(stored.get('experience_brief', {})),
            'capabilities': copy.deepcopy(prior.get('capabilities', [])),
        }

    def _enrich_scoped_routes(self, plan, previous, supplies, impact, usage):
        from .google_route_links import route_links

        affected = set(impact['route_days']) or set(impact['day_numbers'])
        local = copy.deepcopy(plan)
        local['days'] = [day for day in local['days'] if day['day'] in affected]
        try:
            updates = (self.route_builder or route_links)(local, supplies)
        except Exception:
            updates = [{'day': day, 'status': 'unavailable', 'mode': 'driving',
                        'geometry': None, 'legs': [],
                        'note': '本次路网查询未返回；原站点顺序仍保留。'}
                       for day in sorted(affected)]
        by_day = {route.get('day'): route for route in previous.get('routes', [])}
        by_day.update({route.get('day'): route for route in updates
                       if route.get('day') in affected})
        plan['routes'] = [copy.deepcopy(by_day.get(day['day'], {
            'day': day['day'], 'status': 'unavailable', 'mode': 'driving',
            'geometry': None, 'legs': [], 'note': '当日路线尚未生成。'})) for day in plan['days']]
        usage['route_days_available'] = sum(row.get('status') == 'available' for row in plan['routes'])
        usage['route_days_partial'] = sum(row.get('status') == 'partial' for row in plan['routes'])

    @staticmethod
    def _update_scoped_preparation(plan, previous, impact):
        pack = previous.get('preparation_pack')
        if isinstance(pack, dict):
            pack = copy.deepcopy(pack)
            affected_ids = set(impact['preparation_action_ids'])
            for action in pack.get('actions', []):
                if action.get('id') not in affected_ids:
                    continue
                action['report_note'] = '相关行程已变化，已有来源与凭证保留，请重新确认适用性。'
                action['applicability'] = 'needs_reconfirmation'
                if action.get('status') != 'user_reported':
                    action['status'] = 'needs_research'
            if any(action.get('status') == 'needs_research' for action in pack.get('actions', [])):
                pack['status'] = 'partial'
            plan['preparation_pack'] = pack
        if 'preparation_error' in previous:
            plan['preparation_error'] = copy.deepcopy(previous['preparation_error'])

    def prepare(self, trip_id, token, revision, feedback=''):
        """Research preparation without regenerating the user's itinerary."""
        current = self.store.get(trip_id, token)
        if not current.get('plan'):
            raise StateError('NO_PLAN', '请先完成一份旅行方案，再补齐准备细节', 409)
        self._reserve()
        try:
            trip = self.store.begin_revision(trip_id, token, revision)
            trip = self.store.update_job(trip_id, trip['revision'], job_kind='prepare',
                preparation_request=feedback, event='保留你的行程，正在查漏补缺')
            self.pool.submit(self._run_preparation, trip_id, trip['revision'])
            return {'trip': trip}
        except Exception:
            self.slots.release()
            raise

    def message(self, trip_id, token, revision, body):
        from .trip_messages import handle_message
        return handle_message(self, trip_id, token, revision, body)

    def revise(self, trip_id, token, revision, feedback='', locks=None, retry=False, additions=None):
        if retry:
            current = self.store.get(trip_id, token)
            if current.get('job_kind') == 'prepare' and current.get('plan'):
                return self.prepare(trip_id, token, revision, current.get('preparation_request', ''))
        from .scoped_replan import classify_revision
        current = self.store.get(trip_id, token)
        routing = classify_revision(current.get('plan'), feedback, additions)
        routing.update(previous_status=current['status'], previous_error=current.get('error'))
        if assigned_pending_ids(current.get('pending_materials') or [], feedback):
            routing.update(requested_pipeline='needs_research', executed_pipeline='full',
                           fallback_reason='user_material_assignment')
        if current.get('plan'):
            routing.update(executed_pipeline='writer_critic', fallback_reason=None)
        self._reserve()
        try:
            trip = self.store.begin_revision(trip_id, token, revision, feedback, locks, retry, additions)
            self.pool.submit(self._run, trip_id, trip['revision'], routing)
            return {'trip': trip}
        except Exception:
            self.slots.release()
            raise

    def _run_preparation(self, trip_id, revision):
        started, usage = time.monotonic(), {'workflow': 'preparation_action_pack'}
        try:
            trip = self.store.internal_get(trip_id)
            plan = copy.deepcopy(trip['plan'])
            self._prepare_plan(plan, trip, revision, usage, trip.get('source_evidence', []), strict=True)
            usage['duration_ms'] = round((time.monotonic() - started) * 1000)
            self.store.commit_plan(trip_id, revision, plan, usage)
        except Exception as error:
            self._fail(trip_id, revision, model.ModelError('准备研究未完成，原行程保留，可继续重试'), usage, started)
        finally:
            self.slots.release()

    def _prepare_plan(self, plan, trip, revision, usage, sources, strict=False, research_scope=None):
        from .initial_budget import optional_work
        with optional_work(usage if not strict else {}, 'preparation') as allowed:
            if not allowed:
                plan['preparation_error'] = '日程与行动指南已保留；本轮额外来源核查未完成，未知条件仍待查。'
                usage['preparation_status'] = 'incomplete'
                return
            return self._prepare_plan_with_budget(plan, trip, revision, usage, sources, strict, research_scope)

    def _prepare_plan_with_budget(self, plan, trip, revision, usage, sources, strict=False, research_scope=None):
        if not self.preparation_builder:
            if strict:
                raise model.ModelError('准备研究模型未接入')
            return
        def progress(label, **fields):
            self._event(trip['id'], revision, label, **fields)
        progress('日程已保留，正在替你查清入场、交通和必要准备')
        previous = (trip.get('plan') or {}).get('preparation_pack')
        try:
            scoped_options = {}
            if research_scope is not None:
                import inspect
                parameters = inspect.signature(self.preparation_builder).parameters
                accepts_keywords = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
                if not accepts_keywords and not {'research_scope', 'research_evidence'}.issubset(parameters):
                    usage['preparation_scope_compatibility'] = 'unsupported_builder_not_called'
                    raise PlanError('当前准备工具尚不支持限定范围，未扩大研究；原准备记录保留')
                scoped_options = {'research_scope': research_scope, 'research_evidence': sources}
            # Retry can reuse completed public reads, not private model memory.
            # The builder still checks age and applicability for this request.
            if strict:
                sources = list(sources) + (trip.get('preparation_checkpoint') or {}).get('sources', [])
            from .multisource_organize import restrict_staging_reader
            reader = restrict_staging_reader(self.link_reader, plan.get('supplies', []), sources, usage)
            pack, metrics = self.preparation_builder(plan, active_materials(trip),
                sources, self.caller, reader, previous=previous,
                feedback=trip.get('preparation_request', '') if strict else self._context(trip)['latest_request'],
                progress=progress, **scoped_options)
            add_usage(usage, metrics)
            usage['preparation'] = metrics
            from .preparation_state import preserve_reports
            plan['preparation_pack'] = preserve_reports(pack, plan, trip.get('plan'))
            plan.pop('preparation_error', None)
            progress('准备行动已整理；需要本人办理和未查清的事项分别保留',
                     draft_plan=plan if not trip.get('plan') else None)
        except Exception as error:
            if strict:
                raise
            add_failed_usage(usage, error)
            plan.pop('preparation_pack', None)
            plan['preparation_error'] = '本轮准备细节未整理完成，行程保留；可点击查漏补缺继续。'
            usage['preparation_status'] = 'incomplete'

    def _enrich_plan(self, plan, supplies, usage):
        from .initial_budget import optional_work
        with optional_work(usage, 'media-routes') as allowed:
            if not allowed:
                from .google_route_links import route_links
                plan['routes'] = route_links(plan, supplies)
                usage['media_enriched_count'] = 0
                return
            return self._enrich_plan_with_budget(plan, supplies, usage)

    def _enrich_plan_with_budget(self, plan, supplies, usage):
        """Independent source media and route tools enrich an already visible draft."""
        from .google_route_links import route_links
        image_reader = getattr(getattr(self.researcher, 'discovery', None), 'enrich_images', None)
        before = sum(bool(s.get('image_url')) for s in plan['supplies'])
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='journey-enrich') as pool:
            route_job = pool.submit(self.route_builder or route_links, plan, supplies)
            image_job = pool.submit(image_reader, plan['supplies']) if callable(image_reader) else None
            try:
                plan['routes'] = route_job.result()
            except Exception:
                # Enrichment cannot discard a useful draft or invent a route.
                plan['routes'] = [{'day': d['day'], 'status': 'unavailable', 'mode': 'driving',
                    'geometry': None, 'legs': [], 'note': '本次路网查询未返回；站点顺序仍可查看。'}
                    for d in plan['days']]
            if image_job:
                try:
                    plan['supplies'] = image_job.result()
                except Exception:
                    usage['media_enrichment'] = 'unavailable_existing_media_preserved'
        usage['media_enriched_count'] = sum(bool(s.get('image_url')) for s in plan['supplies']) - before
        usage['route_days_available'] = sum(r.get('status') == 'available' for r in plan['routes'])
        usage['route_days_partial'] = sum(r.get('status') == 'partial' for r in plan['routes'])

    def _core_context(self, trip):
        context = self._context(trip)
        previous = copy.deepcopy(trip.get('plan'))
        if previous:
            removed = ('supplies', 'readiness', 'decision_links', 'commitment_checks', 'routes',
                       'preparation_pack', 'preparation_error')
            if not self.autonomous:
                removed += ('understanding', 'reactions', 'questions')
            for key in removed:
                previous.pop(key, None)
            if self.autonomous and previous.get('journey'):
                previous['journey'] = {k: previous['journey'][k] for k in ('goal', 'learning', 'artifacts')
                                       if k in previous['journey']}
        materials = active_materials(trip)
        # Material ledger already contains the brief, additions and feedback with
        # their provenance. Do not send their concatenated duplicates as well.
        return {'materials': materials,
                'current_source_ids': [row['id'] for row in revision_sources(trip)],
                'latest_request': context['latest_request'], 'previous_plan': previous,
                'locked_item_ids': trip['locked_item_ids']}

    def _organize(self, direction, payload, trip, revision, usage, retained_evidence=None):
        if not self.autonomous:
            self._initial_material_details(direction, payload, trip, revision, usage,
                                           trip.get('source_evidence') or [])
            return self.researcher.research(direction['research_queries'],
                previous_supplies=(trip.get('plan') or {}).get('supplies', []))
        from .atomic_orchestration import organize
        from .capability_tools import CapabilityTools, capability_records
        def publish(work):
            ledger = list(retained_evidence or [])
            ledger += [row for row in work['evidence'] if row not in ledger]
            self._event(trip['id'], revision, work['tasks'][-1]['title'] if work['tasks'] else '正在组织资源',
                agent_work={'goal': work['goal'], 'tasks': work['tasks'], 'round': work['round'] + 1},
                source_evidence=ledger,
                discovery={'supplies': work['supplies'], 'phase': 'researching'})
        prior_journey = (trip.get('plan') or {}).get('journey', {})
        context = {**payload, 'previous_supplies': (trip.get('plan') or {}).get('supplies', []),
                   'previous_evidence': trip.get('source_evidence') or [],
                   'previous_tool_tasks': [t for t in prior_journey.get('tasks', [])
                       if t.get('kind') in ('search_supply', 'inspect_source', 'inspect_geography')]}
        from .multisource_organize import admit_staging, restrict_staging_reads
        context = admit_staging(context, usage)
        self._initial_material_details(direction, context, trip, revision, usage,
                                       context['previous_evidence'])
        if 'detail_context' in context:
            payload['detail_context'] = copy.deepcopy(context['detail_context'])
        tools = CapabilityTools(self.researcher, destinations=direction.get('destinations'),
                                place_hints=direction.get('place_hints'))
        tools = restrict_staging_reads(tools, context, usage)
        work = organize(direction, context, tools, self.caller, usage, publish)
        work['capabilities'] = capability_records(work['supplies'], work['evidence'])
        return {'supplies': work['supplies'], 'evidence': work['evidence'], 'work': work,
                'stats': {'candidate_count': len(work['supplies']),
                          'read_count': sum(e.get('status') == 'read' for e in work['evidence']),
                          'source_attempt_count': sum(bool(e.get('source_url')) for e in work['evidence']),
                          'unique_sources_read': len({e.get('source_url') for e in work['evidence'] if e.get('status') == 'read'}),
                          'discovery_attempts': [r.get('result', {}).get('stats', {}).get('discovery')
                              for r in work['results'] if r.get('action', {}).get('tool') == 'search_supply'],
                          'tool_count': len(work['results'])}}

    def _initial_material_details(self, direction, payload, trip, revision, usage, evidence):
        """Retain guide detail for first creation without running revision research."""
        if trip.get('plan'):
            return
        materials = [row for row in payload.get('materials', []) if isinstance(row, dict)
                     and row.get('kind') not in ('user_brief', 'user_feedback')]
        if not materials:
            return
        from .material_details import extract_material_details
        before = copy.deepcopy(usage)

        def account(cumulative):
            # The extractor callback is cumulative, so restore the entry totals
            # before adding it. Persist usage before the next provider call.
            combined = copy.deepcopy(before)
            add_usage(combined, cumulative)
            usage.clear()
            usage.update(combined)
            usage['detail_creation'] = copy.deepcopy(cumulative)
            self.store.update_job(trip['id'], revision, metrics=copy.deepcopy(usage))

        target = {key: copy.deepcopy(direction.get(key)) for key in
                  ('days', 'understanding', 'user_commitments', 'destinations')}
        ledger, detail_usage = extract_material_details(materials, caller=self.caller,
            cached_ledger=trip.get('material_detail_ledger'), on_usage=account,
            read_receipts=evidence, applicability=target)
        account(detail_usage)
        payload['detail_context'] = {'detail_ledger': ledger, 'research_gaps': []}
        self.store.update_job(trip['id'], revision, material_detail_ledger=ledger,
                              metrics=copy.deepcopy(usage))

    @staticmethod
    def _retain_initial_details(plan, decoded):
        """Retain already validated evidence fields across legacy normalization."""
        for clean, original in zip(plan['preparation'], decoded.get('preparation', [])):
            if 'details' in original:
                clean['details'] = copy.deepcopy(original['details'])
        originals = {item['id']: item for day in decoded.get('days', []) for item in day.get('items', [])}
        for day in plan['days']:
            for item in day['items']:
                original = originals[item['id']]
                if 'guide_step_ids' in original:
                    item['guide_step_ids'] = copy.deepcopy(original['guide_step_ids'])

    @staticmethod
    def _catalog_context(supplies):
        fields = ('id', 'name', 'kind', 'city', 'description', 'tags', 'lat', 'lng',
                  'source_url', 'checked_at', 'verification', 'constraints')
        return [{k: s[k] for k in fields if k in s} for s in supplies]

    def _compose(self, payload, supplies, trip, usage, merge_base_plan=None,
                 allowed_impact=None, delivery_work=None, full_scope=None):
        from .initial_contract import SHARD_THRESHOLD
        from .initial_generation import compose_initial, repair_initial
        from .writer_schema import WRITER_PLAN_SCHEMA
        initial = not trip.get('plan')
        initial_sharded = initial and payload['direction']['days'] >= SHARD_THRESHOLD
        initial_checkpoint = copy.deepcopy(trip.get('revision_checkpoint') or {})

        def save_initial(checkpoint):
            nonlocal initial_checkpoint
            self.store.update_job(trip['id'], trip['revision'], revision_checkpoint=checkpoint,
                                  metrics=usage)
            initial_checkpoint = checkpoint

        def compose_shards():
            return compose_initial(caller=self.caller, payload=payload, usage=usage,
                checkpoint=initial_checkpoint, on_checkpoint=save_initial, autonomous=self.autonomous,
                on_progress=lambda progress: self._event(trip['id'], trip['revision'],
                    f'已保存{progress["accepted"]}/{progress["total"]}天草稿，全部校验后统一交付'))

        if self.autonomous:
            from .journey_state import DELTA_CONTRACT, apply_plan_delta
            from .journey_delivery import attach_journey
        original_payload = payload
        if allowed_impact is not None:
            payload = {**payload, 'allowed_impact': copy.deepcopy(allowed_impact)}
        handles = {s['id']: f's{index:03}' for index, s in enumerate(supplies, 1)}
        payload = encode_references(payload, handles)
        if self.autonomous:
            # Tool work is already persisted, not re-sent as duplicated full
            # supply snapshots. The model needs outcomes and unresolved gaps.
            payload['work'] = {k: payload['work'].get(k) for k in
                               ('goal', 'tasks', 'open_gaps', 'experience_brief')}
        # Structural rejection must not spend the one content-repair chance.
        # Both are bounded; each regenerated candidate still validates against
        # the persisted original and its locks, never a failed intermediate.
        run_content_review = self.autonomous and content_review_enabled(
            self.content_review, trip.get('plan'))
        text_review_possible = bool(allowed_impact and allowed_impact.get('semantic_text_contracts'))
        repairs = {'structure': 0, 'semantic': 0, 'truth': 0}
        # Reviewed revisions can accumulate a small structural mistake while
        # applying semantic repairs. Keep that recovery bounded and separate
        # from the semantic budget; the latency-sensitive unreviewed path is
        # still limited to one structural regeneration.
        repair_limits = {'structure': 2 if run_content_review else 1,
                         'semantic': 2 if run_content_review or text_review_possible else 0, 'truth': 1}
        for attempt in range(1 + sum(repair_limits.values())):
            self._event(trip['id'], trip['revision'],
                        '候选与边界已固定，正在编排完整逐日方案' if not attempt
                        else '已定位一处结构问题，正在有界修正')
            prompt = atomic_prompts.compose_messages(payload, DELTA_CONTRACT) if self.autonomous else core_prompts.plan_messages(payload)
            if not self.autonomous and payload.get('detail_context'):
                from .detail_contract import DETAIL_RULES
                prompt[0]['content'] += '\n' + DETAIL_RULES
            if initial_sharded and attempt:
                raw = repair_initial(caller=self.caller, payload=payload,
                    candidate=payload['rejected_proposal'], error=payload['validation_error'], usage=usage)
            elif initial_sharded:
                raw = compose_shards()
            else:
                try:
                    raw, met = self.caller(prompt, max_tokens=16000, timeout=120,
                        reasoning_effort='none', **({'model_role': 'writer',
                        'response_schema': WRITER_PLAN_SCHEMA} if initial else {}))
                    add_usage(usage, met)
                except model.ModelError as error:
                    if not initial or error.usage.get('finish_reason') != 'length':
                        raise
                    add_failed_usage(usage, error)
                    initial_sharded = True
                    raw = compose_shards()
            usage['planning_reasoning_effort'] = 'none'
            raw = display_supply_names(raw, {handles[s['id']]: s['name'] for s in supplies})
            failure_stage = 'structure'
            try:
                proposed = raw
                if initial_sharded:
                    from .revision_plan import validate_revision_plan
                    proposed = validate_revision_plan(raw, {}, encode_references(supplies, handles, ('catalog',)),
                        sources=payload.get('source_evidence', []) + payload.get('materials', []))
                if self.autonomous and trip.get('plan'):
                    raw, representation_fixes = normalize_delta_response(raw)
                    if representation_fixes:
                        usage.setdefault('delta_representation_fixes', []).extend(representation_fixes)
                    if not isinstance(raw.get('delta'), dict):
                        raise PlanError('反馈只能返回delta增量，不允许重新生成完整方案')
                    if allowed_impact is not None:
                        from .scoped_replan import prepare_scoped_response
                        raw = prepare_scoped_response(
                            raw, allowed_impact, merge_base_plan or trip.get('plan'))
                    if full_scope is not None:
                        raw = prepare_full_response(raw, merge_base_plan or trip['plan'], full_scope)
                    proposed = apply_plan_delta(
                        merge_base_plan if merge_base_plan is not None else payload['previous_plan'],
                        raw['delta'], trip['locked_item_ids'])
                    if full_scope is not None:
                        validate_full_result(merge_base_plan or trip['plan'], proposed, full_scope)
                    usage['edit_mode'] = 'local_delta'
                    usage['delta_operation_count'] = sum(len(v) if isinstance(v, (dict, list)) else 1 for v in raw['delta'].values())
                allowed_years, local_repairs = explicit_trip_years(original_payload), []
                decoded = proposal_from_model(proposed, handles, allowed_years,
                                              trip['locked_item_ids'], local_repairs)
                initial_details = not trip.get('plan') and original_payload.get('detail_context')
                if initial_details:
                    from .detail_synthesis import validate_detail_actions
                    decoded = validate_detail_actions(decoded, {}, initial_details)
                if local_repairs:
                    usage.setdefault('local_time_repairs', []).extend(local_repairs)
                if not allowed_years and any(isinstance(day, dict) and day.get('date')
                                             for day in proposed.get('days', [])):
                    usage['calendar_dates_omitted_no_user_year'] = True
                plan = validate_plan(decoded, supplies, trip['plan'], trip['locked_item_ids'],
                                     payload['direction']['days'], '' if self.autonomous else payload['latest_request'])
                if initial_details:
                    self._retain_initial_details(plan, decoded)
                if full_scope is not None:
                    if not self.autonomous:
                        validate_full_result(merge_base_plan or trip['plan'], decoded, full_scope)
                    restore_untouched_fields(trip['plan'], proposed, plan, full_scope)
                if allowed_impact is not None:
                    from .scoped_replan import validate_scoped_result
                    validate_scoped_result(
                        merge_base_plan or trip.get('plan') or {}, plan, allowed_impact)
                if self.autonomous and len({d['category'] for d in plan['decisions']}) != len(plan['decisions']):
                    raise PlanError('同一决定类别只能出现一次，默认与备用请合并到同一choice')
                attach_signature_schedule(decoded, plan)
                checks = commitment_checks(decoded, plan, payload.get('commitment_anchors', []))
                unmet = [x for x in checks if x['status'] == 'unmet']
                if unmet and (self.autonomous or not attempt):
                    usage['last_commitment_checks'] = unmet
                    usage['last_lodging_decisions'] = [
                        {'choice': row.get('choice'), 'supply_ids': row.get('supply_ids', [])}
                        for row in plan.get('decisions', []) if row.get('category') == 'lodging'
                    ]
                    raise PlanError('每日要求仍有遗漏，请围绕锚点联动修改实际日程：' + '；'.join(
                        f"{x['anchor_id']}：{'、'.join(x['issues'])}" for x in unmet))
                plan['understanding'] = copy.deepcopy(original_payload['direction']['understanding'])
                attach_commitments(plan, payload['direction'].get('user_commitments', []), checks)
                if self.autonomous:
                    plan['commitment_schedule'] = decoded.get('commitment_schedule', [])
                    attach_journey(plan, raw, trip.get('plan'), original_payload['direction'],
                                   delivery_work or original_payload['work'], original_payload)
                failure_stage = 'truth'
                usage['delivery_truth'] = validate_delivery(
                    trip.get('plan'), plan, original_payload['latest_request'],
                    fact_correction=usage.get('requested_pipeline') == 'fact_correction')
                from .scoped_replan import scoped_text_changes
                text_changes = scoped_text_changes(merge_base_plan or trip.get('plan') or {},
                                                   plan, allowed_impact or {})
                if text_changes or run_content_review:
                    failure_stage = 'semantic'
                    self._review_candidate(plan, {**original_payload,
                        'scoped_text_changes': text_changes}, trip, usage)
                if self.autonomous:
                    if not (text_changes or run_content_review):
                        usage['content_review'] = ('revision_only' if self.content_review == 'revisions'
                                                   else 'not_run_mvp')
                    if payload['latest_request'] and trip.get('plan') and not substantive_change(trip['plan'], plan):
                        failure_stage = 'structure'
                        raise PlanError('本次反馈没有改变实际安排、决定、材料或待决问题，不能只改标题和说明')
            except PlanError as error:
                if isinstance(error, DeliveryTruthError):
                    usage['delivery_truth'] = error.delivery_truth
                usage['last_rejected_day_dates'] = [
                    {'day': d.get('day'), 'date': str(d.get('date'))[:40]}
                    for d in raw.get('days', [])[:14] if isinstance(d, dict)
                ] if isinstance(raw.get('days'), list) else []
                stage = {'semantic': 'semantic_review', 'truth': 'delivery_truth',
                         'structure': 'structure_validation'}[failure_stage]
                exhausted = (repairs[failure_stage] >= repair_limits[failure_stage]
                             or (not self.autonomous and attempt))
                if failure_stage == 'semantic':
                    from .semantic_review import ReviewProtocolError
                    if isinstance(error, ReviewProtocolError):
                        # A malformed critic response is not evidence that the
                        # candidate plan needs mutation. Preserve the candidate
                        # boundary and fail safely after the critic's own retry.
                        exhausted = True
                if allowed_impact is not None:
                    failure = scoped_failure_details(error, stage)
                    usage.setdefault('validation_rejections', []).append({
                        'attempt': attempt + 1, **failure,
                        'will_retry': not exhausted,
                    })
                    if exhausted:
                        usage['scoped_failure'] = failure
                    # Persist before the next model call, including interrupted
                    # repairs. Field details stay in metrics, not event prose.
                    self.store.update_job(trip['id'], trip['revision'], metrics=usage)
                if failure_stage == 'semantic':
                    from .semantic_review import ReviewProtocolError
                    if isinstance(error, ReviewProtocolError):
                        raise
                if exhausted:
                    raise
                repairs[failure_stage] += 1
                usage['repair_counts'] = {key: value for key, value in repairs.items()
                                          if key != 'truth' or value}
                payload = {**payload, 'rejected_proposal': raw, 'validation_error': str(error)}
                if failure_stage == 'truth':
                    payload['delivery_truth_error'] = copy.deepcopy(error.delivery_truth)
                if allowed_impact is not None:
                    payload['scope_violations'] = copy.deepcopy(
                        getattr(error, 'scope_violations', []))
                usage.setdefault('validation_repairs', []).append(str(error))
                continue
            usage['commitment_unmet_count'] = len(unmet)
            usage['commitment_conditional_count'] = sum(x['status'] == 'conditional' for x in checks)
            # Optional affordances must not gate a useful trip proposal.
            reactions = []
            names = {handles[s['id']]: s['name'] for s in supplies}
            for item in decoded.get('reactions', []) if isinstance(decoded.get('reactions'), list) else []:
                if isinstance(item, dict) and all(isinstance(item.get(k), str) for k in ('label', 'feedback')):
                    if 0 < len(item['label']) <= 40 and 0 < len(item['feedback']) <= 1000:
                        feedback = re.sub(r'\bs\d{3}\b', lambda match: names.get(match[0], match[0]), item['feedback'])
                        reactions.append({'label': item['label'], 'feedback': feedback})
            # Revision affordances have no grounded-claim protocol yet. Do not
            # reintroduce model-authored success claims after the truth gate.
            plan['reactions'] = [] if trip.get('plan') else reactions[:4]
            if initial_sharded:
                initial_checkpoint.update(status='validated', candidate=copy.deepcopy(raw))
                from .revision_plan import plan_sha256
                initial_checkpoint['candidate_sha256'] = plan_sha256(raw)
                initial_checkpoint['usage'] = copy.deepcopy(usage)
                save_initial(initial_checkpoint)
            return plan
        raise PlanError('本轮修正预算已用完，原方案保留')

    def _review_candidate(self, plan, context, trip, usage):
        """Optional evaluation mode, not on the latency-sensitive MVP path."""
        from .semantic_review import ReviewProtocolError, review_messages, review_result
        self._event(trip['id'], trip['revision'], '正在核对同行需求、每日衔接与随身材料是否一致')
        validation_error = ''
        reviewed = (focused_plan(plan, context['full_revision_scope'])
                    if context.get('full_revision_scope', {}).get('kind') == 'focused_revision' else plan)
        for reviewer_attempt in range(2):
            critique, metrics = self.caller(
                review_messages(reviewed, reviewed.get('journey', {}), context, validation_error),
                max_tokens=6000, timeout=75, reasoning_effort='low')
            add_usage(usage, metrics)
            usage['semantic_review_calls'] = usage.get('semantic_review_calls', 0) + 1
            try:
                review = review_result(critique, plan, context)
                if review.get('protocol_repairs'):
                    usage['semantic_review_bounded_repairs'] = (
                        usage.get('semantic_review_bounded_repairs', 0)
                        + len(review['protocol_repairs']))
                    usage.setdefault('semantic_review_protocol_repairs', []).extend(
                        review['protocol_repairs'])
                break
            except ReviewProtocolError as error:
                if reviewer_attempt:
                    raise
                validation_error = str(error)
                usage['semantic_review_schema_repairs'] = (
                    usage.get('semantic_review_schema_repairs', 0) + 1)
        usage['semantic_review_reasoning_effort'] = 'low'
        if review['issues']:
            raise PlanError('内容一致性需要修正：' + '；'.join(
                x['issue'] + ' → ' + x['repair'] for x in review['issues']))
        plan.setdefault('journey', {}).setdefault('tasks', [])
        plan['journey']['quality_review'] = review
        for index, gap in enumerate(review['external_gaps'], 1):
            plan['journey']['tasks'].append({'id': f'content-gap-{index}',
                'title': gap['issue'], 'kind': 'supply', 'status': 'needs_confirmation',
                'detail': gap['condition'], 'supply_ids': [], 'evidence_ids': []})
