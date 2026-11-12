"""Production revise orchestration: one writer/critic loop, one atomic publish."""
import copy
import time

from .full_revision_scope import build_full_revision_scope, extend_full_revision_scope
from .multisource_organize import admit_staging
from .pending_materials import pending_after_assessment, revision_sources
from .revision_plan import validate_revision_plan
from .validation import PlanError
from .writer_critic import RevisionRejected, revise_plan
from .writer_shards import progress_summary
from .revision_stages import StageTrace
from .revision_presentation import presentation_input, attach_revision_presentation


def run_revision(service, trip_id, revision, routing):
    started = time.monotonic()
    usage = {'workflow': 'writer_critic_diff', 'executed_pipeline': 'writer_critic',
             'requested_pipeline': routing.get('requested_pipeline'),
             'full_necessity': {'reason': 'Model selects necessary context and exact edits; full plan is only merged and validated locally.'},
             'writer_calls': 0, 'critic_calls': 0, 'model_calls': 0,
             'input_tokens': 0, 'output_tokens': 0}
    def stage_event(event):
        usage['stages'] = copy.deepcopy(tracker.stages)
        service._event(trip_id, revision, event['label'], revision_stage=tracker.current)
    tracker = StageTrace(stage_event)
    try:
        trip = service._initialize_materials(service.store.internal_get(trip_id), revision)
        previous = copy.deepcopy(trip['plan'])
        if not previous:
            raise PlanError('尚无已有方案可修改，请先完成首次方案')
        trip = service._read_images(trip, revision, usage)
        trip = service._read_links(trip, revision)
        feedback = service._context(trip)['latest_request']
        try:
            hint = build_full_revision_scope(previous, routing)
            incoming = revision_sources(trip, feedback)
            hint = extend_full_revision_scope(previous, routing, hint, incoming)
            pool = pending_after_assessment(trip.get('pending_materials') or [], incoming, hint)
        except PlanError:
            # An unresolved hint is evidence for the writer/critic, never an
            # authority to block the request or erase user material.
            hint = {'reason': '影响范围未能确定，仅供writer和critic判断', 'day_numbers': []}
            pool = list(trip.get('pending_materials') or [])
        # Only descriptive scope reaches the writer; legacy field permissions do not.
        hint = {key: hint[key] for key in ('day_numbers', 'item_ids', 'domains',
                'affected_node_ids', 'impact_source') if key in hint}
        hint['advisory_only'] = True
        service.store.update_job(trip_id, revision, pending_materials=pool)
        context = admit_staging({
            'materials': copy.deepcopy(trip.get('material_sources') or []),
            'previous_supplies': copy.deepcopy(previous.get('supplies') or []),
            'previous_evidence': copy.deepcopy(trip.get('source_evidence') or []),
        }, usage)
        candidates = context['previous_supplies']
        evidence = context['previous_evidence']
        # Structured detail ingestion/research is owned by the detail synthesis
        # module; the writer sees indexed, relevant records rather than a ledger dump.
        from .detail_synthesis import build_revision_details, validate_detail_actions, prompt_detail_context
        details = build_revision_details(service, trip, revision, previous, feedback, usage,
            materials=context['materials'], evidence=evidence, tracker=tracker)
        usage['calls'] = copy.deepcopy(usage.get('detail_synthesis', {}).get('calls', []))
        context['materials'], evidence = details['materials'], details['evidence']
        detail_context = {key: details.get(key, {} if key == 'detail_ledger' else [])
                          for key in ('detail_ledger', 'research_gaps')}
        sources = context['materials'] + evidence
        source_usage = copy.deepcopy(usage)

        def publish(engine_usage):
            usage.update(copy.deepcopy(engine_usage))
            usage['calls'] = copy.deepcopy(source_usage.get('calls', []) + engine_usage.get('calls', []))
            usage['stages'] = copy.deepcopy(tracker.stages)
            for field in ('model_calls', 'input_tokens', 'output_tokens'):
                left, right = source_usage.get(field), engine_usage.get(field)
                usage[field] = sum(value for value in (left, right) if type(value) is int)
            if source_usage.get('usage_complete') is False or engine_usage.get('usage_complete') is False:
                usage['usage_complete'] = False
            usage['duration_ms'] = round((time.monotonic() - started) * 1000)
            service.store.update_job(trip_id, revision, metrics=copy.deepcopy(usage))

        def progress(event):
            phase = event.get('stage', '') if isinstance(event, dict) else ''
            label = ('正在核对实际修改是否落实你的要求' if phase == 'critic'
                     else '保留原方案，正在根据你的要求编写新方案')
            service._event(trip_id, revision, label)

        def checkpoint(value):
            service.store.update_job(trip_id, revision, revision_checkpoint=value,
                                     revision_progress=progress_summary(value))

        def validate(raw):
            checked = validate_revision_plan(presentation_input(previous, raw), previous, candidates,
                locks=trip.get('locked_item_ids', []), sources=sources)
            result = validate_detail_actions(checked, previous, detail_context)
            return attach_revision_presentation(previous, checked if result is None else result)

        candidate, engine_usage = revise_plan(
            previous=previous, feedback=feedback, materials=context['materials'],
            candidates=candidates, evidence=evidence, impact_hint=hint,
            pending_materials=pool, caller=tracker.wrap(service.caller),
            validate_candidate=validate,
            on_progress=progress, on_usage=publish, scoped=True, detail_context=prompt_detail_context(detail_context),
            checkpoint=trip.get('revision_checkpoint'), on_checkpoint=checkpoint,
            request_id=trip.get('revision_request_id'),
            request_constraints={'locked_item_ids': trip.get('locked_item_ids', [])},
            traveler_profile=trip.get('traveler_profile') or {},
            candidate_review=getattr(service, 'phase1_candidate_review', None),
            budget_seconds=max(0, 480 - (time.monotonic() - started)))
        publish(engine_usage)
        from .coach_gate import (detect_execution_mode, normalize_memory,
                                 record_correction, record_locked_decision)
        memory = normalize_memory(trip.get('coach_memory'))
        if routing.get('requested_pipeline') == 'fact_correction':
            # 螺纹开裂式记忆：用户纠正过的坑沉淀为 per-trip 永久检查点。
            memory = record_correction(memory, feedback, revision)
        execution_mode = detect_execution_mode(feedback)
        if execution_mode:
            memory = record_locked_decision(memory, feedback, revision)
        from .revision_receipt_runtime import prepare_receipt
        receipt = prepare_receipt(service, trip_id, revision, previous, candidate,
            feedback, engine_usage['revision_review'], usage, tracker,
            detail_context=detail_context, evidence=evidence, materials=context['materials'],
            traveler_profile=trip.get('traveler_profile') or {}, deadline=started + 480,
            started=started, coach={'memory': memory, 'execution_mode': execution_mode})
        # No preparation builder, summary formatter, or media writer may alter
        # this candidate after critic approval. The store binds the exact hash.
        service.store.commit_reviewed_plan(trip_id, revision, candidate, usage,
            engine_usage['revision_review'], source_evidence=evidence, revision_receipt=receipt,
            coach_memory=memory)
    except RevisionRejected as error:
        # Engine usage is already cumulative and persisted; _fail must not add
        # the exception's same tokens a second time.
        service._fail(trip_id, revision, PlanError(str(error)), usage, started)
    except Exception as error:
        service._fail(trip_id, revision, error, usage, started)
    finally:
        service.slots.release()
