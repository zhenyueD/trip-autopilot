"""One charged scoped revision generation; shared reasoning and raw call ledger."""
from __future__ import annotations

import asyncio
import json
import os

import httpx2 as httpx
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.usage import UsageLimits

from .comparison_card_budget import private_json
from .model_boundary import boundary_output_type
from .comparison_card_model import RecordedTransport
from .intent_revision import IntentRevisionDraft, SLIM_PROMPT, SYSTEM_PROMPT, _request_id
from .runtime_model_policy import runtime_model_settings
from .strong_reasoning import MODEL, TIMEOUT_SECONDS, build_strong_reasoning_model, strong_reasoning_settings


class IntentRevisionModel:
    def __init__(self, budget, *, api_key=None, transport_factory=None, max_tokens=None,
                 settings_override=None, runtime_slim=False):
        self.budget, self.api_key = budget, api_key
        self.transport_factory = transport_factory
        self.max_tokens, self.settings_override = max_tokens, dict(settings_override or {})
        self.runtime_slim = runtime_slim
        if runtime_slim and (max_tokens is not None or settings_override):
            raise ValueError('Slim revision uses its task policy without legacy retry overrides')

    def __call__(self, *, request_id, payload):
        return asyncio.run(self.generate(request_id=request_id, payload=payload))

    async def generate(self, *, request_id, payload):
        _request_id(request_id)
        api_key = self.api_key or os.environ.get('DEEPSEEK_API_KEY')
        if not api_key:
            raise ValueError('DEEPSEEK_API_KEY is missing')
        artifacts = self.budget.directory / 'model-artifacts'
        transport = RecordedTransport(self.budget, request_id, artifacts,
            self.transport_factory() if self.transport_factory else None,
            **({'input_stage': 'revision'} if self.runtime_slim else {}))
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_SECONDS, follow_redirects=False) as http:
            client = AsyncOpenAI(api_key=api_key, base_url='https://api.deepseek.com',
                                 max_retries=0, http_client=http)
            model = build_strong_reasoning_model(DeepSeekProvider(openai_client=client))
            settings = (runtime_model_settings('revision_patch') if self.runtime_slim
                        else strong_reasoning_settings('revision'))
            if self.max_tokens is not None:
                if type(self.max_tokens) is not int or not 1 <= self.max_tokens <= 24000:
                    raise ValueError('Revision generation cap must be at most 24000')
                settings['max_tokens'] = self.max_tokens
            if self.settings_override:
                if (len(self.settings_override) != 1 or
                        set(self.settings_override) - {'max_tokens', 'reasoning_effort'}):
                    raise ValueError('A retry may override only one permitted configuration field')
                cap = self.settings_override.get('max_tokens', settings['max_tokens'])
                effort = self.settings_override.get('reasoning_effort', settings['thinking'])
                if type(cap) is not int or not 1 <= cap <= settings['max_tokens'] or effort not in ('max', 'high'):
                    raise ValueError('Retry configuration exceeds the approved limits')
                settings.update(max_tokens=cap, thinking=effort)
            agent = Agent(model, output_type=boundary_output_type(IntentRevisionDraft, request_id=request_id, artifacts=artifacts), retries=0,
                          system_prompt=SYSTEM_PROMPT + ('\n\n' + SLIM_PROMPT if self.runtime_slim else ''),
                          model_settings=settings)
            result = await agent.run(json.dumps(payload, ensure_ascii=False),
                                     usage_limits=UsageLimits(request_limit=1))
        revision = result.output.model_dump()
        private_json(artifacts / f'{request_id}-validated.json', {
            'revision': revision, 'model': MODEL, 'pydantic_usage': str(result.usage)})
        return revision
