"""Native model/tool adapters for the five-stage orchestration.

Research branches have isolated message windows, sharing the existing durable
request/tool accounting. Component validators remain the final authority.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from .comparison_card_budget import private_json
from .single_session_document import DocumentStore, _display_text
from .single_session_pdf import PRINT_CSS, render_pdf
from .writer_tokenizer import WriterTokenizerError
from .single_session_tool_calls import degradation_feedback


def _without_source_text(value):
    """Copy a model payload while retaining full evidence in runtime state."""
    if isinstance(value, dict):
        return {key: _without_source_text(item) for key, item in value.items()
                if key != 'source_text'}
    if isinstance(value, list):
        return [_without_source_text(item) for item in value]
    return deepcopy(value)


def _without_url_query(value):
    return urlunsplit(urlsplit(value)._replace(query=''))


async def layout_preflight(spec, directory):
    """Exercise the actual document/PDF mechanism before authoring any prose."""
    directory = Path(directory) / 'layout-preflight'
    directory.mkdir(parents=True, exist_ok=True)
    document = DocumentStore(directory)
    headings = spec['constraints']['sections']
    result = document.save('# 版式骨架\n\n' + '\n\n'.join('## ' + x for x in headings))
    receipt = await asyncio.to_thread(render_pdf, result['html_path'], directory / 'skeleton.pdf',
                                      allow_remote_images=False)
    return {'passed': True, 'status': 'ready', 'html_path': result['html_path'], 'pdf': receipt,
            'print_css_sha256': hashlib.sha256(PRINT_CSS.encode()).hexdigest()}


class NativeOrchestrationBackend:
    def __init__(self, agent):
        self.agent = agent
        self.evidence_ids = {}
        self.research_parse_outcomes = {}
        self.research_search_outcomes = {}

    async def _stage(self, name, instruction, data, *, research=False):
        from .single_session_agent import SYSTEM, TOOLS, STAGE_TOOL
        messages = [{'role':'system','content': SYSTEM + '\n\n本轮是独立的编排阶段。'
                     '以下阶段规则覆盖上文的自由编排与save指令：' + instruction +
                     '\n只能使用本阶段工具；用 finish_stage(result_json) 返回完整JSON。'},
                    {'role':'user','content':json.dumps(data,ensure_ascii=False)}]
        available = ([t for t in TOOLS if t['function']['name'] in {'search','read_page'}]
                     if research else []) + [STAGE_TOOL]
        # Historical calls must remain registered for every tokenizer preflight.
        # Retry permissions constrain new calls independently of that full schema.
        allowed_names = {t['function']['name'] for t in available}
        token = self.agent._stage_payload.set({'messages': messages, 'tools':available,
                                               'search_resilience': research})
        path = self.agent.directory / 'orchestration' / 'model-stages' / name
        namespace = uuid4().hex
        parse_errors = []
        try:
            while True:
                private_json(path / 'messages.json', messages)
                try:
                    message, reason = await self.agent._model()
                except WriterTokenizerError as exc:
                    if not research or not parse_errors:
                        raise
                    parse_errors.append({'type': type(exc).__name__, 'message': str(exc)})
                    outcome = self.research_parse_outcomes[data['category']]
                    outcome.update(status='degraded', parse_error_count=len(parse_errors))
                    result = {'candidates': []}
                    private_json(path / 'parse-status.json', outcome)
                    private_json(path / 'result.json', result)
                    return result
                message = deepcopy(message)
                for call in message.get('tool_calls') or []:
                    call['id'] = namespace + ':' + call['id']
                messages.append(message)
                calls = message.get('tool_calls') or []
                if not calls and reason == 'tool_calls_degraded':
                    messages.append(degradation_feedback())
                    continue
                if reason == 'length' or not calls:
                    raise RuntimeError('Orchestration stage did not finish: ' + name)
                names = [c['function']['name'] for c in calls]
                if any(n not in allowed_names for n in names):
                    raise ValueError('Tool not permitted in stage ' + name)
                if 'finish_stage' in names:
                    if len(calls) != 1:
                        raise ValueError('Stage finish must follow all research results')
                    try:
                        result = json.loads(json.loads(calls[0]['function']['arguments'])['result_json'])
                    except json.JSONDecodeError as exc:
                        if not research:
                            raise
                        parse_errors.append({'type': type(exc).__name__, 'message': exc.msg,
                                             'position': exc.pos})
                        outcome = {'category': data['category'], 'retry_count': 1,
                                   'parse_error_count': len(parse_errors), 'errors': parse_errors,
                                   'status': 'retrying' if len(parse_errors) == 1 else 'degraded',
                                   'verification_status': 'unverified'}
                        self.research_parse_outcomes[data['category']] = outcome
                        messages.append({'role': 'tool', 'tool_call_id': calls[0]['id'],
                            'content': 'finish_stage JSON parsing failed. Resubmit finish_stage once '
                                       'with valid JSON using the existing research only.'
                                       if len(parse_errors) == 1 else
                                       'Category degraded: research result unverified; no candidates admitted.'})
                        private_json(path / 'messages.json', messages)
                        private_json(path / 'parse-status.json', outcome)
                        if len(parse_errors) == 1:
                            # Retry only the finish submission, never repeat external research.
                            allowed_names = {'finish_stage'}
                            continue
                        result = {'candidates': []}
                        private_json(path / 'result.json', result)
                        return result
                    if parse_errors:
                        outcome = self.research_parse_outcomes[data['category']]
                        outcome['status'] = 'recovered'
                        private_json(path / 'parse-status.json', outcome)
                    messages.append({'role':'tool','tool_call_id':calls[0]['id'],'content':'Stage recorded.'})
                    private_json(path / 'messages.json', messages)
                    private_json(path / 'result.json', result)
                    return result
                results = await self.agent._execute_calls(calls)
                for call, result in zip(calls, results):
                    if isinstance(result, BaseException):
                        raise result
                    if research and 'search_transport' in result:
                        self.research_search_outcomes.setdefault(data['category'], []).append(
                            deepcopy(result['search_transport']))
                        private_json(path / 'search-status.json',
                                     self.research_search_outcomes[data['category']])
                    messages.append({'role':'tool','tool_call_id':call['id'],
                                     'content':json.dumps(result,ensure_ascii=False)})
        finally:
            self.agent._stage_payload.reset(token)

    async def freeze(self, intake):
        self.research_parse_outcomes.clear()
        self.research_search_outcomes.clear()
        return await self._stage('freeze',
            '规格冻结，不研究。返回 destination, departure_date(ISO), season_scope, categories'
            '(数组：开放/票价/交通/住宿/日照/联系方式/其他，其他含图片来源)，trip_parameters。'
            '按本次目的地实际需要选择类别，不预设城市模板。缺日期可用null并保留待问；'
            '不得伪造用户决定。reusable_scopes默认只有season_scope；不要为缓存命中放宽当前旅行季。', intake)

    async def research(self, spec, category):
        self.research_parse_outcomes.pop(category, None)
        self.research_search_outcomes.pop(category, None)
        result = await self._stage('research-' + category,
            '仅研究指定事实类别，独立返回 {candidates: [...]}。候选格式：'
            '{atom:{destination,claim,category,s_level,source_name,source_url,verbatim_quote,'
            'checked_date,season_scope,expires_on,evidence_kind:"recorded",notes:[]},source_id,authority,scope_key}。'
            'source_id必须来自read_page；引文必须逐字。scope_key标同实体同声明条件。'
            'checked_date沿用原文读取时间；expires_on保守排复核日，不伪造有效期。'
            '搜索摘要不算原文，单源/UGC/旧季就地保留未核范围。没有原文则返回空候选并记录缺口。',
            {'spec':spec,'category':category}, research=True)
        candidates = result['candidates']
        for candidate in candidates:
            source = self.agent.documents.sources[candidate['source_id']]
            if _without_url_query(candidate['atom']['source_url']) != _without_url_query(source['url']):
                raise ValueError('Research source URL differs from recorded read')
            candidate['atom']['source_url'] = source['url']
            candidate['source_text'] = source['content']
            candidate['atom']['checked_date'] = datetime.fromisoformat(
                source['retrieved_at'].replace('Z', '+00:00')).date().isoformat()
        return candidates

    async def adjudicate(self, spec, candidates):
        return await self._stage('adjudicate',
            '独立汇总裁决，不调用研究或写作。只按输入引用返回 '
            '{accepted:[{candidate_id,source_id,s_level,expires_on,notes}],'
            'rejected:[{candidate_id,source_id,reason}],'
            'decisions:[{candidate_id,source_id,reason}]}。'
            '每个候选必须恰好出现在accepted或rejected一次，并在decisions中恰好一次，'
            'reason说明采用或拒绝理由。candidate_id与source_id必须原样引用输入；'
            '同一source_id可含多个候选，以candidate_id区分。不得返回atom或source_text。'
            '逐组核实体、日期、条件：当季压往年、官方压转述、冬季专项压夏季通用；'
            '不得多数表决。旧季只能当明确历史参考，冲突不能解决则留未知。'
            '只能选候选，不能新增或改写其断言/引文/来源；可明确指定s_level/expires_on/notes。'
            '需要续核替代时保留研究候选已有supersedes，不为多得原子绕判重。'
            '给拒绝理由、采用理由；研究缺口不能写成已核事实。', {'spec':spec,'candidates':_without_source_text(candidates),
            'research_gaps': spec.get('research_gaps', self.research_gaps(spec['categories']))})

    def research_gaps(self, categories):
        gaps = [deepcopy(item) for category, item in self.research_parse_outcomes.items()
                if category in categories and item['status'] == 'degraded']
        for category in categories:
            failed = [deepcopy(item) for item in self.research_search_outcomes.get(category, [])
                      if item['status'] == 'degraded']
            if failed:
                gaps.append({'category': category, 'status': 'degraded',
                             'verification_status': 'unverified',
                             'reason': 'search_transport_unavailable', 'searches': failed})
        return gaps

    async def preflight(self, spec, directory):
        return await layout_preflight(spec, directory)

    def _bind(self, adjudicated):
        for candidate in adjudicated['accepted']:
            atom = candidate['atom']
            sid = candidate.get('source_id')
            if sid not in self.agent.documents.sources:
                sid = 'atom-' + atom['atom_id']
                source = candidate.get('source_text') or atom['verbatim_quote']
                self.agent.documents.register_source(sid, atom['source_url'], source,
                                                      atom['checked_date'])
            # Ordinary facts bind here. Price amounts are chosen by the writer
            # from these same admitted quotes, never from excluded research.
            record = self.agent._record_evidence(sid, atom['verbatim_quote'], atom['claim'], 'other')
            self.evidence_ids[atom['atom_id']] = {'source_id':sid,'evidence':record}

    async def compose(self, spec, adjudicated):
        self._bind(adjudicated)
        result = await self._stage('compose',
            '只根据spec与裁决集成文；不能使用未准入事实或模型记忆补事实。'
            '将research_gaps逐类写入「出发前先核这几条」，明确未核内容与需补核事项；'
            '不得将研究失败写成已确认事实或承诺自动跟进。'
            '返回 {markdown,fact_atom_ids:[],estimates:[],self_check:{passed:true,findings:[]},prices:[]}。'
            '采用已有六节结构和destination-detail-v1。普通事实用给定evidence.marker。'
            '金额先在prices声明 {atom_id,unit_amount,currency,quantity,unit:人|车|份,classification:verified|estimate}，'
            '正文用占位 [[price:0]] 等，绑定由程序原组件完成。unit_amount必须逐字来自该atom引文。'
            '非关键费用是参考估算：只放overview.estimated_items或明确估算段，附乘式及假设；'
            '未知单价不填数字。每个估算另记 {estimate_id,label:"estimate",source_atom_ids:[]}，'
            '现有金额显示仅支持人/车/份；按晚/日/小时的金额先保持未知并记接口缺口，不能换错单位。'
            '不得同时归入fact_atom_ids(仅已核事实)或verified_items。估算不可用已核事实口吻；'
            'self_check记录逐项防污染自查，不合格passed=false。不得把当季未知改成已核。',
            {'spec':spec,'adjudicated':_without_source_text(adjudicated),'bindings':deepcopy(self.evidence_ids)})
        for index, price in enumerate(result.pop('prices', [])):
            atom = next(c['atom'] for c in adjudicated['accepted'] if c['atom']['atom_id']==price['atom_id'])
            if price['classification'] not in {'verified','estimate'}:
                raise ValueError('Unknown price classification')
            estimated_sources = {sid for item in result['estimates']
                                 for sid in item.get('source_atom_ids', [])}
            if ((price['classification'] == 'estimate') != (price['atom_id'] in estimated_sources)
                    or (price['classification'] == 'verified'
                        and price['atom_id'] not in result['fact_atom_ids'])):
                raise ValueError('Price classification differs from composition accounting')
            binding = self.evidence_ids[price['atom_id']]
            record = self.agent.documents.record_evidence(binding['source_id'],atom['verbatim_quote'],
                        atom['claim'],'price',price['unit_amount'],price['currency'],price['quantity'])
            ledger = self.agent.documents._read_json('ledger.json')
            shown = _display_text(record['marker'], ledger, None)[2][0]
            if price.get('unit') != shown['basis']:
                raise ValueError('Price unit unsupported by unchanged renderer')
            result['markdown'] = result['markdown'].replace('[[price:'+str(index)+']]',record['marker'])
        # Inspect the completed, bound document in a separate model step, not
        # merely the author's promised self-check embedded in its draft.
        result['self_check'] = await self._stage('contamination-check',
            '审阅已完成成文和价格绑定的实际文档，仅返回 {passed:boolean,findings:[]}。'
            '逐项检查：事实只来自裁决集；估算有标签、假设和乘式，未进入已核项，'
            '未用事实口吻陈述；历史季节/单源未知未升级；未核价格没有确定金额。'
            '检查失败给具体位置和原因，passed=false。不要重写、不要研究。',
            {'spec':spec,'adjudicated':_without_source_text(adjudicated),'composition':deepcopy(result)})
        return result

    async def render(self, spec, composition, adjudicated, directory):
        result = self.agent._save_generated_document('save_handbook',
                 {'markdown':composition['markdown']}, 'orchestration-save')
        pdf = await asyncio.to_thread(render_pdf, result['html_path'],
                                      Path(result['html_path']).with_suffix('.pdf'), allow_remote_images=True)
        self.agent.supply.saved()
        return {**result,'document_saved':True,'pdf':pdf}
