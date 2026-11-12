"""One candidate call and one critic call; no correction may consume critic budget."""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Annotated

import httpx2 as httpx
from openai import AsyncOpenAI
from pydantic import BeforeValidator
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.usage import UsageLimits
from pydantic_ai.exceptions import UnexpectedModelBehavior

from .comparison_card_budget import private_json, BudgetExhausted
from .model_boundary import recorded_model_output
from .comparison_card_model import RecordedTransport
from .strong_reasoning import MODEL, TIMEOUT_SECONDS, build_strong_reasoning_model, strong_reasoning_settings
from .supply_candidates import (SYSTEM_PROMPT, SupplyCandidates, SupplySeed,
                                validate_candidate_sources, audit_candidate_claims)
from .supply_evidence_floor import apply_evidence_floor
from .supply_claim_review import SupplyClaimCritic
from .supply_output_normalization import normalize_candidate_evidence


def candidate_reasoning_settings():
    """Leave shared planning/C1 controls intact; reserve room for candidate output."""
    return {**strong_reasoning_settings('supply_candidates'), 'thinking': 'high'}


class SupplyCandidatesModel:
    def __init__(self, budget, *, api_key=None, transport_factory=None):
        self.budget = budget
        self.api_key = api_key
        self.transport_factory = transport_factory

    def __call__(self, enriched, *, request_id):
        return asyncio.run(self.generate(enriched, request_id=request_id))

    async def generate(self, enriched, *, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', request_id):
            raise ValueError('Invalid supply request ID')
        SupplySeed.model_validate(enriched)
        prompt = json.dumps(enriched, ensure_ascii=False)
        api_key = self.api_key or os.environ.get('DEEPSEEK_API_KEY')
        if not api_key:
            raise ValueError('DEEPSEEK_API_KEY is missing')
        artifacts = self.budget.directory / 'model-artifacts'
        if self.budget.snapshot()['remaining'] < 2:
            raise BudgetExhausted('Candidate and critic require two remaining physical calls')
        try:
            result = await self._attempt(enriched, prompt, request_id, api_key, artifacts)
        except UnexpectedModelBehavior as error:
            private_json(artifacts / f'{request_id}-validation-failure.json', {
                'error': type(error).__name__, 'correction_available': False})
            raise
        candidate_set = result.output.model_dump()
        validate_candidate_sources(enriched, candidate_set)
        private_json(artifacts / f'{request_id}-validated.json', {
            'candidate_set': candidate_set, 'model': MODEL, 'pydantic_usage': str(result.usage),
            'accepted_attempt_id': request_id, 'semantic_truth_verified': False})
        mechanical = audit_candidate_claims(enriched, candidate_set)
        private_json(artifacts / f'{request_id}-mechanical.json', mechanical)
        critic = SupplyClaimCritic(self.budget, api_key=api_key,
                                   transport_factory=self.transport_factory)
        try:
            review = await asyncio.to_thread(critic.review, enriched, candidate_set,
                                             request_id=f'{request_id}-critic')
        except Exception as error:
            annotated, audit = apply_evidence_floor(enriched, candidate_set,
                                                    mechanical_audit=mechanical)
            private_json(artifacts / f'{request_id}-unreviewed-candidates.json', annotated)
            private_json(artifacts / f'{request_id}-unreviewed-audit.json', audit)
            raise
        annotated, audit = apply_evidence_floor(enriched, candidate_set,
                                                mechanical_audit=mechanical, critic_audit=review)
        self.last_audit = audit
        private_json(artifacts / f'{request_id}-claim-audit.json', audit)
        private_json(artifacts / f'{request_id}-annotated.json', annotated)
        return annotated

    async def _attempt(self, enriched, prompt, request_id, api_key, artifacts):
        # Each physical attempt has its own raw artifact and global budget lease.
        transport = RecordedTransport(self.budget, request_id, artifacts,
                                      self.transport_factory() if self.transport_factory else None)
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_SECONDS, follow_redirects=False) as http:
            client = AsyncOpenAI(api_key=api_key, base_url='https://api.deepseek.com',
                                 max_retries=0, http_client=http)
            model = build_strong_reasoning_model(DeepSeekProvider(openai_client=client))
            def normalize_output(raw):
                # Save both representations before the strict schema can reject.
                private_json(artifacts / f'{request_id}-generated-raw.json', raw)
                canonical, provenance = normalize_candidate_evidence(raw, enriched)
                private_json(artifacts / f'{request_id}-normalization.json', provenance)
                private_json(artifacts / f'{request_id}-canonical.json', canonical)
                return recorded_model_output(canonical, SupplyCandidates, request_id=request_id,
                                             artifacts=artifacts, preserve_raw=False)

            output_type = Annotated[SupplyCandidates, BeforeValidator(normalize_output)]
            agent = Agent(model, output_type=output_type, retries=0, system_prompt=SYSTEM_PROMPT,
                          model_settings=candidate_reasoning_settings())

            @agent.output_validator
            def validate_output(output: SupplyCandidates):
                try:
                    validate_candidate_sources(enriched, output.model_dump())
                except ValueError as error:
                    raise ModelRetry(str(error)) from error
                return output

            return await agent.run(prompt, usage_limits=UsageLimits(request_limit=1))
