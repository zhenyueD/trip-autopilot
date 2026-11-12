"""Single charged intent-generation request using the shared recorded transport."""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import httpx2 as httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from pydantic_ai import Agent
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.usage import UsageLimits

from .comparison_card_budget import private_json
from .model_boundary import boundary_output_type
from .comparison_card_model import RecordedTransport
from .intent_questions import IntentQuestions, SYSTEM_PROMPT
from .strong_reasoning import MODEL, TIMEOUT_SECONDS, build_strong_reasoning_model, strong_reasoning_settings


class ConfirmedItem(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=5000)

    @field_validator('id', 'text')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Confirmed information must not be blank')
        return value


class TripContext(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    confirmed: list[ConfirmedItem] = Field(max_length=100)
    current_plan: JsonValue = None
    unresolved: JsonValue = None

    @model_validator(mode='after')
    def unique_confirmed(self):
        if len({item.id for item in self.confirmed}) != len(self.confirmed):
            raise ValueError('Confirmed IDs must be unique')
        return self


def load_case(seed_path, case_id):
    seed = json.loads(Path(seed_path).read_text(encoding='utf-8'))
    if not isinstance(seed, dict):
        raise ValueError('Intent seed must be an object')
    if 'cases' in seed:
        if not isinstance(seed['cases'], list) or not all(isinstance(case, dict) for case in seed['cases']):
            raise ValueError('Intent seed cases must be objects')
        matches = [case for case in seed['cases'] if case.get('id') == case_id]
        if len(matches) != 1:
            raise ValueError('Exactly one matching intent case is required')
        seed = matches[0]
    brief = seed.get('brief')
    if not isinstance(brief, str) or not brief.strip() or len(brief) > 12000:
        raise ValueError('A nonblank intent brief of at most 12000 characters is required')
    TripContext.model_validate(seed.get('trip_context'))
    return seed


def validate_request(request_id, previous, feedback, answer_id):
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', request_id):
        raise ValueError('Invalid intent request ID')
    if feedback is not None and (not isinstance(feedback, str) or not feedback.strip() or len(feedback) > 4000):
        raise ValueError('Feedback must be nonblank text of at most 4000 characters')
    prior = IntentQuestions.model_validate(previous) if previous is not None else None
    if answer_id is not None:
        choices = {choice.id for question in prior.questions for choice in question.choices} if prior else set()
        if not isinstance(answer_id, str) or answer_id not in choices:
            raise ValueError('Answer ID must identify a choice from the previous intent card')


class IntentQuestionsModel:
    def __init__(self, budget, seed_path, *, case_id=None, api_key=None, transport_factory=None):
        self.budget, self.seed_path = budget, Path(seed_path)
        self.case_id, self.api_key = case_id, api_key
        self.transport_factory = transport_factory

    def __call__(self, request_id, previous=None, feedback=None, answer_id=None):
        return asyncio.run(self.generate(request_id=request_id, previous=previous,
                                         feedback=feedback, answer_id=answer_id))

    async def generate(self, request_id, previous=None, feedback=None, answer_id=None):
        validate_request(request_id, previous, feedback, answer_id)
        seed = load_case(self.seed_path, self.case_id)
        api_key = self.api_key or os.environ.get('DEEPSEEK_API_KEY')
        if not api_key:
            raise ValueError('DEEPSEEK_API_KEY is missing')
        payload = {'brief': seed['brief'], 'trip_context': seed['trip_context'],
                   'previous_card': previous, 'explicit_feedback': feedback, 'answer_id': answer_id}
        artifacts = self.budget.directory / 'model-artifacts'
        transport = RecordedTransport(self.budget, request_id, artifacts,
                                      self.transport_factory() if self.transport_factory else None)
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_SECONDS, follow_redirects=False) as http:
            client = AsyncOpenAI(api_key=api_key, base_url='https://api.deepseek.com',
                                 max_retries=0, http_client=http)
            model = build_strong_reasoning_model(DeepSeekProvider(openai_client=client))
            agent = Agent(model, output_type=boundary_output_type(IntentQuestions, request_id=request_id, artifacts=artifacts), retries=0,
                          system_prompt=SYSTEM_PROMPT, model_settings=strong_reasoning_settings('intent_questions'))
            result = await agent.run(json.dumps(payload, ensure_ascii=False),
                                     usage_limits=UsageLimits(request_limit=1))
        card = result.output.model_dump()
        private_json(artifacts / f'{request_id}-validated.json', {
            'card': card, 'model': MODEL, 'pydantic_usage': str(result.usage)})
        return card
