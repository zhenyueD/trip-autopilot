"""C5 assembly advice and read-only review using the accepted recorded model path."""
from __future__ import annotations

import asyncio
import copy
import json
import os
from typing import Literal

import httpx2 as httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.usage import UsageLimits

from .comparison_card_budget import private_json
from .model_boundary import boundary_output_type
from .delivery_consistency import DeliveryAlignment, ALIGNMENT_PROMPT
from .comparison_card_model import RecordedTransport
from .intent_revision import _request_id
from .strong_reasoning import TIMEOUT_SECONDS, build_strong_reasoning_model, strong_reasoning_settings


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Connection(Record):
    id: str
    day: int
    from_item_id: str | None
    to_item_id: str | None
    origin: str
    destination: str
    date: str
    timezone: str
    mode: str
    walking: str
    waiting: str
    transfer: str
    ride: str
    buffer: str
    cost: str
    basis: str
    source_ids: list[str]
    status: Literal['unknown', 'estimate', 'supported']


class BudgetLine(Record):
    id: str
    label: str
    amount: float | None
    currency: str
    quantity: int = Field(ge=1)
    basis: str
    source_ids: list[str]
    item_ids: list[str]


class ExternalTask(Record):
    id: str
    text: str
    item_ids: list[str]
    status: Literal['external_pending', 'unknown', 'user_confirmed']
    owner: str
    trigger: str


class Claim(Record):
    id: str
    text: str
    risk: Literal['S1', 'S2']
    source_id: str | None
    quote: str | None
    checked_at: str | None
    status: Literal['unknown', 'unverified', 'supported']


class Unknown(Record):
    id: str
    text: str
    impact: str
    recheck_when: str
    item_ids: list[str]


class AssemblyNotes(Record):
    judgment: str
    connections: list[Connection]
    budget: list[BudgetLine]
    tasks: list[ExternalTask]
    claims: list[Claim]
    unknowns: list[Unknown]


class AlignedAssemblyNotes(AssemblyNotes):
    delivery_alignment: DeliveryAlignment


class ReviewFinding(Record):
    id: str
    verdict: Literal['pass', 'fail', 'unknown']
    risk: Literal['S1', 'S2', 'quality']
    basis: str
    affected_object: str
    needed: str


class AssemblyReview(Record):
    judgment: str
    criteria: list[ReviewFinding]
    claims: list[ReviewFinding]
    outcome: Literal['ready', 'partial', 'failed']


class CaseReview(AssemblyReview):
    case_id: str


class BatchReview(Record):
    cases: list[CaseReview]


ASSEMBLY_PROMPT = """把输入的已有比较、意图提问、正式计划组织成可用旅行手册的补充资料。只生成 AssemblyNotes 工具结构，不重写正式计划、不调用工具、不研究新来源。
这是历史数据回放，非真实旅行保障。人物、日期和地点由输入决定。confirmed 是冻结输入，不是你新查到的事实。comparison_card 推荐不等于用户已选，intent_card 默认是假设，工程回放答案不等于真人新确认。
主张有来源才引用；只允许输入 sources 的 id 和逐字原文。摘要只证明摘要里写过，不能伪装官方原文/重新核验。source.checked_at 是旧记录时间，不得改成今天。不可知的拥挤、座位、可用性不保证。供应缺证保留未知，不为填满手册编造营业、入口、报价或地址。
judgment 一句话说明本手册的主取舍。connections 覆盖抵达、每天首末段与必要衔接、退房行李与返程登机前，标清日期时区；绑定已有项目 id，旅途边界可用 null。walking/waiting/transfer/ride/buffer 分开写，建议窗口和估算就明说估算，不当实测；查不到写未知。origin/destination 对酒店只引用用户订单酒店，未核实英文对应不借用外部属性。day 为对应正式计划天数。
budget 分项列住宿、交通、门票等，只有输入中有计价依据才能填 amount，否则 null；quantity 清楚，currency 不可乱换。不把预算范围当报价。外部事务 tasks 保留 owner 与 trigger，状态不写已解决或已预订；user_confirmed 仅适用于明确已有用户答复。
claims 精选改变决定的外部事实或待核实问题，逐条 S1/S2；unknown/unverified/supported 如实，空缺 source_id/quote/checked_at 用 null，supported 也仍须后续 critic 核验。内部计划建议不是已确认外部事实。
unknowns 写影响哪项决定、什么条件下再核查，关联 item_ids。不重问已确认的信息；必要问题至多三项，不让用户承担研究。
所有正文简洁。严格返回工具发布字段，根节点直接是 judgment/connections/budget/tasks/claims/unknowns，不增加 payload、note 或其他字段。每项文字尽量一句，避免复述整份行程。"""

REVIEW_PROMPT = """你只读审查这份历史旅行组装产物，不能修改计划、来源、阈值或成功标准。检查是否真正生效，不因为字段存在就通过。
criteria 必须逐项报告六项 id：executable、trustworthy、tradeoffs、cheap_to_verify、editable、intent。每项 pass/fail/unknown、原文依据、受影响对象与需要补充什么。unknown 不算通过。无真人计时就不能证明验收省时；但可说明阅读结构。仅有工程演示不能冒充真人价值通过。
claims 必须按输入 claim id 逐条审查所有 S1/S2，结合实际来源原文、日期条件、逐字归属记录与身份边界作语义判断。摘要/转载/模型生成不自动是独立官方证据；没有证据就 unknown。来源字段存在不等于支持断言。无据肯定 S1 必须 fail。
首末段须包括抵达、行李、退房、机场交通、值机到登机前。估算必须就地标，预算未知不能填零。C1推荐不能变用户选择；C2默认不能变用户答案；工程回放选择不能变真人确认。
核对修订前后正式计划/真实diff/手册是否一致；撤销是否保留后续无关编辑。有测试不代表此场景真实执行。应用/撤销回执以环境保存结果为准。调用失败和预算不足也不能把原目标降为通过。
outcome 仅当原六项和关键主张均有证据通过才 ready；有真实缺口但可交付标注草案用 partial；存在关键错误用 failed。输出一次 AssemblyReview 工具结构，根节点直接 judgment/criteria/claims/outcome，无额外包装或字段，不重试。"""

REVIEW_BATCH_PROMPT = REVIEW_PROMPT.replace(
    '输出一次 AssemblyReview 工具结构，根节点直接 judgment/criteria/claims/outcome，无额外包装或字段，不重试。',
    '输入是多份独立 case。正式逐日计划在 handbook.sections.daily，其余正式计划字段在 plan_metadata；二者合起来是送审计划。'
    '输出一次 BatchReview 工具结构，根节点只有 cases；每项含原 case_id、judgment、criteria、claims、outcome。'
    '每个 case 独立覆盖六项标准和该 case 的所有 claim id。只用该 case 的来源与证据，不借另一 case 补齐缺失资料，不混算结论。'
    '无额外包装或字段，不重试。')


class AssemblyModel:
    def __init__(self, budget, *, api_key=None, transport_factory=None, settings_override=None,
                 fixture_bindings=(), runtime_slim=False):
        self.budget, self.api_key, self.transport_factory = budget, api_key, transport_factory
        self.settings_override = dict(settings_override or {})
        self.fixture_bindings = copy.deepcopy(tuple(fixture_bindings))
        self.runtime_slim = runtime_slim

    def __call__(self, *, request_id, payload, mode='assemble'):
        return asyncio.run(self.generate(request_id=request_id, payload=payload, mode=mode))

    def preflight(self, *, fixture, approval, output_path):
        from .run_reliability import configured_run, require_run_preflight
        config = configured_run(('assembly', 'review', 'revision'))
        if 'max_tokens' in self.settings_override:
            for row in config['stages']:
                if row['stage'] in ('assembly', 'review'):
                    row['max_tokens'] = self.settings_override['max_tokens']
        return require_run_preflight(fixtures=[fixture], approvals=[approval],
                                     fixture_bindings=self.fixture_bindings,
                                     config=config, output_path=output_path)

    async def generate(self, *, request_id, payload, mode='assemble'):
        _request_id(request_id)
        if mode not in ('assemble', 'review', 'review_batch'):
            raise ValueError('Unsupported assembly operation')
        key = self.api_key or os.environ.get('DEEPSEEK_API_KEY')
        if not key:
            raise ValueError('DEEPSEEK_API_KEY is missing')
        artifacts = self.budget.directory / 'model-artifacts'
        stage = 'assembly' if mode == 'assemble' else 'review'
        transport = RecordedTransport(self.budget, request_id, artifacts,
            self.transport_factory() if self.transport_factory else None,
            input_stage=stage if self.runtime_slim else None)
        output = {'assemble': AssemblyNotes, 'review': AssemblyReview,
                  'review_batch': BatchReview}[mode]
        prompt = {'assemble': ASSEMBLY_PROMPT, 'review': REVIEW_PROMPT,
                  'review_batch': REVIEW_BATCH_PROMPT}[mode]
        if mode == 'assemble' and 'delivery_context' in payload:
            output = AlignedAssemblyNotes
            prompt = prompt.replace('根节点直接是 judgment/connections/budget/tasks/claims/unknowns',
                '根节点直接是 judgment/connections/budget/tasks/claims/unknowns/delivery_alignment')
            prompt += '\n\n' + ALIGNMENT_PROMPT

        if self.runtime_slim:
            from .runtime_stage_input import project_stage_input
            payload = project_stage_input(payload, stage=stage)
            private_json(artifacts / f'{request_id}-projected-input.json', payload)

        try:
            async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_SECONDS,
                                        follow_redirects=False) as http:
                client = AsyncOpenAI(api_key=key, base_url='https://api.deepseek.com',
                                     max_retries=0, http_client=http)
                model = build_strong_reasoning_model(DeepSeekProvider(openai_client=client))
                settings = strong_reasoning_settings('assembly' if mode == 'assemble' else 'review')
                if self.settings_override:
                    if (len(self.settings_override) != 1 or
                            set(self.settings_override) - {'max_tokens', 'reasoning_effort'}):
                        raise ValueError('A retry may override only one permitted configuration field')
                    cap = self.settings_override.get('max_tokens', settings['max_tokens'])
                    effort = self.settings_override.get('reasoning_effort', settings['thinking'])
                    if type(cap) is not int or not 1 <= cap <= settings['max_tokens'] or effort not in ('max', 'high'):
                        raise ValueError('Retry configuration exceeds the approved limits')
                    settings.update(max_tokens=cap, thinking=effort)
                agent = Agent(model, output_type=boundary_output_type(output, request_id=request_id, artifacts=artifacts),
                    retries=0, system_prompt=prompt,
                    model_settings=settings)
                result = await agent.run(json.dumps(payload, ensure_ascii=False,
                                        separators=(',', ':') if self.runtime_slim else None),
                                        usage_limits=UsageLimits(request_limit=1))
            value = result.output.model_dump()
            private_json(artifacts / f'{request_id}-validated.json', value)
            return value
        except Exception as error:
            private_json(artifacts / f'{request_id}-failure.json',
                         {'type': type(error).__name__, 'message': str(error), 'mode': mode})
            raise
