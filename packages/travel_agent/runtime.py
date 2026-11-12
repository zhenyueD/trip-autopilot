"""Bounded model→evidence tools→proposal→validation loop, persisted between steps."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from .phase1_budget_context import ContextThreadPoolExecutor as ThreadPoolExecutor

from . import model, prompts
from .consistency import apply_review, check_confirmation, editable_fields
from .intake import MAX_IMAGE_TEXT_CHARS, add_sources, check_source_budget, source_support, validate_intake
from .material_reader import read_public_link
from .store import StateError, TripStore
from .supply import Catalog, compact_catalog, refresh_source
from .validation import PlanError, validate_plan, validate_preview


def add_usage(total, usage):
    incomplete = usage.get('usage_complete') is False
    for key in ('model_calls', 'input_tokens', 'output_tokens'):
        value = usage.get(key)
        if type(value) is int and value >= 0:
            known = total.get(key)
            total[key] = (known if type(known) is int and known >= 0 else 0) + value
        elif key in usage or incomplete:
            total.setdefault(key, None)
            incomplete = True
        else:
            total.setdefault(key, 0)
    if incomplete:
        total['usage_complete'] = False
    elif usage.get('usage_complete') is True:
        total.setdefault('usage_complete', True)
    for key in ('structured_output_retries', 'model_truncations',
                'failed_model_invocations', 'provider_fallbacks'):
        if usage.get(key):
            total[key] = total.get(key, 0) + usage[key]
    if usage.get('finish_reason') == 'length':
        total['model_truncations'] = total.get('model_truncations', 0) + 1
    if usage.get('provider_fallback_reason'):
        total['provider_fallback_reason'] = usage['provider_fallback_reason']
        if not usage.get('provider_fallbacks'):
            total['provider_fallbacks'] = total.get('provider_fallbacks', 0) + 1
    if usage.get('model'):
        total['model'] = usage['model']


def add_failed_usage(total, error):
    failed_usage = getattr(error, 'usage', {})
    add_usage(total, failed_usage)
    if isinstance(error, model.ModelError) and failed_usage.get('model_calls'):
        total['failed_model_invocations'] = total.get('failed_model_invocations', 0) + 1


class StudioService:
    def __init__(self, store=None, caller=None, catalog=None, refresher=None, link_reader=None):
        self.store = store or TripStore()
        self.caller = caller or model.call_json
        self.catalog = catalog or Catalog()
        self.refresher = refresher or refresh_source
        self.link_reader = link_reader or read_public_link
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='travel-agent')
        self.slots = threading.BoundedSemaphore(2)
        self.store.recover_interrupted()

    def start(self, brief, evidence_text='', images=None, links=None, documents=None):
        self._reserve()
        try:
            trip, token = self.store.create(brief, evidence_text, images or [], links, documents)
            self.pool.submit(self._run, trip['id'], trip['revision'])
            return {'trip': trip, 'token': token}
        except Exception:
            self.slots.release()
            raise

    def revise(self, trip_id, token, revision, feedback='', locks=None, retry=False, additions=None):
        self._reserve()
        try:
            trip = self.store.begin_revision(trip_id, token, revision, feedback, locks, retry, additions)
            self.pool.submit(self._run, trip_id, trip['revision'])
            return {'trip': trip}
        except Exception:
            self.slots.release()
            raise

    def _reserve(self):
        if not self.slots.acquire(blocking=False):
            raise StateError('CAPACITY', '正在处理其他旅行，请稍后再试，未产生新任务', 429)

    def _event(self, trip_id, revision, label, **fields):
        self.store.update_job(trip_id, revision, event=label, **fields)

    def _run(self, trip_id, revision):
        started, usage = time.monotonic(), {'prompt_version': prompts.VERSION}
        try:
            trip = self.store.internal_get(trip_id)
            trip = self._initialize_materials(trip, revision)
            trip = self._read_images(trip, revision, usage)
            trip = self._read_links(trip, revision)
            self._event(trip_id, revision, '正在理解同行情境、已有安排和真正想获得的体验')
            catalog = compact_catalog(self.catalog.get_supply([s['id'] for s in self.catalog._items]))
            context = self._context(trip)
            research, intake = self._discover({**context, 'catalog': catalog}, trip, usage)
            self._event(trip_id, revision, '已形成待复核的体验方向，正在核对供给条件')
            evidence = self._research_sources(research, trip_id, revision)
            self._event(trip_id, revision, '供给来源检查已记录；未取得实时库存或预订确认', source_evidence=evidence)
            payload = {**context, 'research': research, 'catalog': catalog,
                       'intake': intake,
                       'source_refresh': evidence, 'locked_item_ids': trip['locked_item_ids']}
            self._event(trip_id, revision, '正在把体验、路线、住宿和准备事项组织成一趟旅行')
            raw, met = self.caller(prompts.plan_messages(payload), max_tokens=10000)
            add_usage(usage, met)
            plan = self._check_and_repair(raw, payload, trip, revision, usage)
            intake, plan = self._review_consistency(intake, plan, payload, trip, revision, usage)
            usage['duration_ms'] = round((time.monotonic() - started) * 1000)
            usage['source_checks'] = len(evidence)
            self.store.commit_plan(trip_id, revision, plan, usage,
                intake={**intake, 'revision': revision}, preview=validate_preview(research, intake),
                research={**research, 'intake': intake})
        except (model.ModelError, PlanError, StateError, ValueError, KeyError, TypeError) as error:
            self._fail(trip_id, revision, error, usage, started)
        except Exception:
            self._fail(trip_id, revision, model.ModelError('处理遇到异常，已有方案保留，可以重试'), usage, started)
        finally:
            self.slots.release()

    def _context(self, trip):
        base_feedback = (trip.get('revision_base') or {}).get('feedback', [])
        latest = trip['feedback'][-1] if trip['feedback'] and trip['feedback'] != base_feedback else ''
        if 'revision_feedback' in trip:
            latest = trip['revision_feedback']
        old_sources = ((trip.get('revision_base') or {}).get('material_state') or {}).get('material_sources') or []
        previous_ids = {x['id'] for x in old_sources}
        has_additions = (bool(trip['revision_material_ids']) if 'revision_material_ids' in trip
                         else any(x['id'] not in previous_ids for x in trip.get('material_sources', [])))
        if not latest and trip['revision'] > 1 and has_additions:
            latest = '请结合本轮新增材料更新旅行，保留没有被新材料纠正的有效要求。'
        return {'brief': trip['brief'], 'provided_material': trip['evidence_text'],
                'material_sources': trip.get('material_sources', []),
                'feedback_history': trip['feedback'], 'previous_plan': trip['plan'],
                'latest_request': latest,
                'scope': '开放目的地出发前准备；不得虚构库存或确认状态'}

    def _initialize_materials(self, trip, revision):
        if trip.get('material_sources') is not None:
            return trip
        sources = add_sources([], revision=1, brief=trip['brief'], images=trip['images'])
        if trip['evidence_text']:
            sources.append({'id': 'legacy', 'kind': 'legacy_material', 'label': '旧版材料（来源未分离）',
                            'status': 'read', 'content': trip['evidence_text']})
        self._event(trip['id'], revision, '正在整理已有材料', material_sources=sources)
        return self.store.internal_get(trip['id'])

    def _discover(self, payload, trip, usage):
        attempts, research = [], None
        for attempt in range(2):
            instruction = prompts.INTAKE_REPAIR if attempt else prompts.DISCOVERY
            raw, met = self.caller(prompts.messages(instruction, payload), max_tokens=5500)
            add_usage(usage, met)
            research = ({**research, 'intake': raw.get('intake'),
                         'questions': raw.get('questions', research.get('questions', []))}
                        if attempt else raw)
            try:
                if not isinstance(research.get('intake'), dict) or 'date_window' not in research['intake']:
                    raise PlanError('intake缺少date_window：明确日期需带真实引文；日期仍未知时填null，不编造')
                intake = validate_intake(research.get('intake'), trip['material_sources'],
                                         research.get('questions', []), require_disposition=True)
                research = self._calendar_mission(research, intake)
            except PlanError as error:
                self._record_intake(trip, attempts, raw, str(error))
                if attempt:
                    raise PlanError('材料的引用或条件尚未对齐，本轮未生成方案；已保留材料，可以重试或补充') from None
                usage.setdefault('intake_repairs', []).append('材料引用或结构需局部修复')
                payload = {'repair_target': 'intake_only', 'material_sources': trip['material_sources'],
                           'rejected_intake': raw.get('intake'), 'intake_error': str(error),
                           'source_support': source_support(raw.get('intake'), trip['material_sources']),
                           'questions': raw.get('questions', [])}
                self._event(trip['id'], trip['revision'], '正在核对理解所引用的材料，避免把猜测当事实')
            else:
                self._record_intake(trip, attempts, raw)
                return research, intake

    @staticmethod
    def _calendar_mission(research, intake):
        options = [o for c in intake.get('conflicts', []) if 'dates' in c.get('fields', [])
                   for o in c.get('date_options', [])]
        if options:
            lengths = {o['duration_days'] for o in options}
            days = lengths.pop() if len(lengths) == 1 else None
            mission = {**(research.get('mission') or {}), 'days': days, 'dates': '全程日期未确定，候选互斥'}
            return {**research, 'mission': mission, 'calendar_adjustment': {
                'window_days': days, 'reason': '日期候选不能合并；仅以共同天数排相对日程，不选择某份材料为真。'
                '候选天数不同则天数也未知，只能明示可调整的相对日假设。'}}
        window = intake.get('date_window')
        if not window:
            return research
        mission = dict(research.get('mission') or {})
        days = mission.get('days')
        if days is not None and (type(days) is not int or not 1 <= days <= 14):
            raise PlanError('本试点仅支持1—14天，不能因住宿窗口更短就擅自缩短整趟旅行')
        if days is None or days < window['duration_days']:
            mission['days'] = window['duration_days']
            return {**research, 'mission': mission, 'calendar_adjustment': {
                'previous_days': days, 'window_days': window['duration_days'],
                'reason': '必须包含窗口首尾日期；住宿晚数不等于旅行日历天数，航班仍需单独确认'}}
        return research

    def _record_intake(self, trip, attempts, raw, error=None):
        encoded = json.dumps(raw, ensure_ascii=False)
        record = {'revision': trip['revision'], 'error': error[:2000] if error else None,
                  'error_sha256': hashlib.sha256(error.encode()).hexdigest() if error else None,
                  'error_truncated': bool(error and len(error) > 2000),
                  'sha256': hashlib.sha256(encoded.encode()).hexdigest()}
        record.update({'candidate': raw} if len(encoded) <= 90000 else
                      {'candidate_omitted': '超过诊断存储上限，保留hash；不宣称完整原稿已保存'})
        attempts.append(record)
        self.store.update_job(trip['id'], trip['revision'], intake_attempts=attempts[-2:])

    def _read_links(self, trip, revision):
        sources = trip['material_sources']
        pending = [x for x in sources if x['kind'] == 'web_link' and x['status'] == 'pending']
        if not pending:
            return trip
        self._event(trip['id'], revision, f'正在读取{len(pending)}条公开收藏链接，不使用账号登录态')
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(self.link_reader, [x['url'] for x in pending]))
        for source, result in zip(pending, results):
            source.update(status=result['status'], content=result.get('text', '') if result['status'] == 'read' else '',
                          message=result.get('message', ''), fetched_at=result.get('fetched_at'),
                          body_sha256=result.get('body_sha256'), truncated=result.get('truncated', False))
            if result.get('title'):
                source['label'] = result['title'][:160]
        check_source_budget(sources)
        self._event(trip['id'], revision, '收藏链接的读取结果已记录；未读内容不会冒充已了解', material_sources=sources)
        return self.store.internal_get(trip['id'])

    def _read_images(self, trip, revision, usage):
        if not trip['images']:
            return trip
        self._event(trip['id'], revision, '正在读取你分享的图片；猜测与订单事实会分开记录')
        parts = [{'type': 'text', 'text': '图片文件名：' + ', '.join(x['name'] for x in trip['images'])}]
        parts += [{'type': 'image_url', 'image_url': {'url': x['data_url']}} for x in trip['images']]
        extracted, met = self.caller([{'role': 'system', 'content': prompts.VISION},
                                      {'role': 'user', 'content': parts}], vision=True, max_tokens=2800)
        add_usage(usage, met)
        image_text = json.dumps(extracted, ensure_ascii=False)
        if len(image_text) > MAX_IMAGE_TEXT_CHARS:
            raise PlanError('图片提取结果超过处理上限，原图仍保留；请重试，不会静默删掉材料')
        # Separate model-extracted observations from the user's literal words.
        sources = trip['material_sources']
        for source in sources:
            if source['kind'] == 'image_extracted' and source['status'] == 'pending':
                source.update(content=image_text, status='image_extracted',
                              message='图片提取结果，非用户确认或实时订单核验')
        check_source_budget(sources)
        self._event(trip['id'], revision, '图片已解析，原图已移除；正在结合文字和收藏理解旅行', material_sources=sources, images=[])
        return self.store.internal_get(trip['id'])

    def _research_sources(self, research, trip_id, revision):
        valid = {x['id'] for x in self.catalog._items}
        ids = research.get('refresh_ids', [])
        if not isinstance(ids, list):
            raise PlanError('研究步骤没有返回有效来源列表')
        chosen = [x for x in ids if isinstance(x, str) and x in valid][:2]
        evidence = []
        for id_ in chosen:
            item = self.catalog.get_supply([id_])[0]
            self._event(trip_id, revision, '正在读取官方来源：' + item['name'])
            evidence.append(self.refresher(id_))
        return evidence

    def _check_and_repair(self, raw, payload, trip, revision, usage):
        days = (payload.get('research', {}).get('mission') or {}).get('days')
        if days is not None and (type(days) is not int or not 1 <= days <= 14):
            raise PlanError('当前试点支持 1—14 天；不能未经你同意缩短旅行，请调整范围')
        self._event(trip['id'], revision, '检查供给引用、时间顺序、锁定项与准备状态')
        try:
            return validate_plan(raw, self.catalog._items, trip['plan'], trip['locked_item_ids'], days,
                                 payload.get('latest_request', ''), payload.get('intake'))
        except PlanError as error:
            usage.setdefault('validation_repairs', []).append(str(error))
            self._event(trip['id'], revision, '发现一处不一致，正在修正；原方案保持可读')
            repair = {**payload, 'rejected_proposal': raw, 'validation_error': str(error)}
            fixed, met = self.caller(prompts.plan_messages(repair), max_tokens=10000)
            add_usage(usage, met)
            return validate_plan(fixed, self.catalog._items, trip['plan'], trip['locked_item_ids'], days,
                                 payload.get('latest_request', ''), payload.get('intake'))

    def _review_consistency(self, intake, plan, payload, trip, revision, usage):
        bundle = {'intake': intake, 'plan': plan}
        attempts = []
        for attempt in range(2):
            self._event(trip['id'], revision, '交付前复核：材料理解、默认方向与整趟安排是否一致')
            review_payload = {k: payload[k] for k in ('material_sources', 'latest_request',
                'research', 'catalog', 'source_refresh', 'locked_item_ids') if k in payload}
            # Discovery prose is not a second authority for current facts.
            review_payload['research'] = {k: payload['research'][k] for k in
                ('mission', 'calendar_adjustment') if k in payload['research']}
            review_payload.update(phase='consistency_review', bundle=bundle,
                                  editable_fields=editable_fields(bundle))
            instruction = prompts.CONSISTENCY_CONFIRM if attempt else prompts.CONSISTENCY
            review, met = self.caller(prompts.messages(instruction, review_payload),
                                      max_tokens=1600 if attempt else 4000)
            add_usage(usage, met)
            usage['consistency_reviews'] = attempt + 1
            self._record_consistency(trip, revision, attempts, review)
            if attempt:
                check_confirmation(review)
                return bundle['intake'], bundle['plan']
            corrected, count = apply_review(bundle, review, allow_idempotent=True)
            if not count:
                return bundle['intake'], bundle['plan']
            current = corrected['intake']
            # Normalized disposition stores evidence rather than the raw quote
            # key. Restore the validator's input shape, then recheck it against
            # the original source; no model can patch either field.
            for note in current.get('source_notes', []):
                if note['outcome'] in ('context_only', 'needs_clarification'):
                    note['quote'] = note['evidence'][0]['quote']
            current = validate_intake(current, trip['material_sources'], current.get('questions', []),
                                      require_disposition=True)
            checked = validate_plan(corrected['plan'], self.catalog._items, trip['plan'],
                trip['locked_item_ids'], payload['research'].get('mission', {}).get('days'),
                payload.get('latest_request', ''), current)
            usage['consistency_patches'] = count
            bundle = {'intake': current, 'plan': checked}

    def _record_consistency(self, trip, revision, attempts, review):
        encoded = json.dumps(review, ensure_ascii=False)
        record = {'revision': revision, 'sha256': hashlib.sha256(encoded.encode()).hexdigest()}
        record.update({'candidate': review} if len(encoded) <= 50000 else
                      {'candidate_omitted': '超过诊断上限，仅保留hash，不代表完整原稿'})
        attempts.append(record)
        self.store.update_job(trip['id'], revision, consistency_attempts=attempts[-2:])

    def _fail(self, trip_id, revision, error, usage, started):
        add_failed_usage(usage, error)
        usage['duration_ms'] = round((time.monotonic() - started) * 1000)
        try:
            self.store.update_job(trip_id, revision, status='needs_attention' if isinstance(error, PlanError) else 'error',
                                  error=str(error)[:500], metrics=usage,
                                  event='本轮尚未完成；已有结果保留，可调整或重试')
        except StateError:
            pass  # Deleted or superseded jobs cannot recreate or overwrite a trip.
