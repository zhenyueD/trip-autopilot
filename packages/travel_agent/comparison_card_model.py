"""One durably charged card-generation request, with optional prior research."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path

import httpx2 as httpx
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.usage import UsageLimits

from .comparison_card import ComparisonCard, SYSTEM_PROMPT
from .comparison_card_budget import private_json
from .model_boundary import boundary_output_type
from .factuality_policy import prepare_verification_context
from .model_response_diagnostics import redact_recorded_body
from .strong_reasoning import MODEL, TIMEOUT_SECONDS, build_strong_reasoning_model, strong_reasoning_settings
from .writer_tokenizer import count_writer_text, measure_writer_input


def measure_comparison_request(payload):
    """Official tokenizer body count; tool protocol overhead is only estimated."""
    effort = payload.get('reasoning_effort', 'none')
    messages = measure_writer_input(payload['messages'], model=payload['model'], reasoning_effort=effort)
    tools_json = json.dumps(payload.get('tools', []), ensure_ascii=False, separators=(',', ':'))
    tools_tokens = count_writer_text(tools_json, model=payload['model'])
    return {**messages, 'reasoning_effort': effort, 'tools_json_tokens': tools_tokens,
            'local_combined_estimate': messages['preflight_input_tokens'] + tools_tokens,
            'tools_framing_status': 'JSON body counted; provider tool framing is not reproduced',
            'provider_input_tokens': None}


class RecordedTransport(httpx.AsyncBaseTransport):
    def __init__(self, budget, request_id, artifacts, inner=None, *, input_stage=None):
        self.budget, self.request_id, self.artifacts = budget, request_id, Path(artifacts)
        self.inner = inner or httpx.AsyncHTTPTransport(retries=0)
        self.sent = False
        self.input_stage = input_stage

    async def handle_async_request(self, request):
        if self.sent:
            raise RuntimeError('Automatic second HTTP request is forbidden')
        self.sent = True
        payload = json.loads(request.content)
        authorization = request.headers.get('authorization', '')
        secrets = [authorization[7:] if authorization.lower().startswith('bearer ') else authorization,
                   request.headers.get('x-api-key'), request.headers.get('x-goog-api-key')]
        preflight = measure_comparison_request(payload)
        if self.input_stage is not None:
            from .runtime_model_policy import input_admission, RuntimeInputLimitExceeded
            admission = input_admission(self.input_stage, preflight)
            preflight['stage_input_admission'] = admission
        private_json(self.artifacts / f'{self.request_id}-preflight.json', preflight)
        if self.input_stage is not None and not admission['passed']:
            raise RuntimeInputLimitExceeded(
                f"{self.input_stage} input preflight rejected: "
                f"{admission['local_count_plus_reserve']} > {admission['input_limit_tokens']}; not_sent")
        lease = self.budget.reserve('llm', self.request_id)
        self.budget.record_request(lease, model=payload.get('model'))
        started = time.monotonic()
        request_evidence = {
            'lease_id': lease, 'method': request.method, 'url': str(request.url),
            'body': payload, 'request_headers': 'omitted'}
        private_json(self.artifacts / f'{self.request_id}-request.json', json.loads(
            redact_recorded_body(json.dumps(request_evidence).encode('utf-8'), secrets)))
        try:
            response = await self.inner.handle_async_request(request)
            raw = await response.aread()
        except Exception as error:
            try:
                self.budget.settle(lease, status='unknown', error=type(error).__name__,
                                   elapsed_ms=round((time.monotonic() - started) * 1000))
            except Exception as accounting_error:
                error.add_note('Attempt accounting failed: ' + type(accounting_error).__name__)
            raise
        elapsed = round((time.monotonic() - started) * 1000)
        evidence, model = self._response_evidence(response, raw, lease, elapsed, secrets)
        self._save_response(response, evidence, model)
        return response

    def _response_evidence(self, response, raw, lease, elapsed, secrets):
        from .model import _request_id_evidence
        safe_body = redact_recorded_body(raw, secrets)
        try:
            safe_parsed = json.loads(safe_body)
        except ValueError:
            safe_parsed = {}
        safe_parsed = safe_parsed if isinstance(safe_parsed, dict) else {}
        provider_id = response.headers.get('x-request-id') or safe_parsed.get('id')
        for secret in secrets:
            provider_id = _request_id_evidence(provider_id, secret).get('provider_request_id')
        usage = safe_parsed.get('usage') if isinstance(safe_parsed.get('usage'), dict) else None
        safe_raw = raw if safe_body == raw.decode('utf-8', errors='replace') else safe_body.encode('utf-8')
        error_body = ({} if response.is_success else {
            'provider_error_body': safe_body, 'provider_error_body_status': 'complete',
            'provider_error_body_truncated': False})
        return {
            'lease_id': lease, 'http_status': response.status_code,
            'provider_request_id': provider_id, 'elapsed_ms': elapsed,
            'usage': usage, 'body_utf8': safe_body,
            'body_base64': base64.b64encode(safe_raw).decode('ascii'),
            'body_redacted': safe_body != raw.decode('utf-8', errors='replace'), **error_body}, safe_parsed.get('model')

    def _save_response(self, response, evidence, model):
        evidence_path = self.artifacts / f'{self.request_id}-response.json'
        metadata, write_error = {'http_status': response.status_code}, None
        try:
            private_json(evidence_path, evidence)
            metadata.update(response_evidence=str(evidence_path), response_evidence_status='saved')
        except Exception as error:
            write_error = error
            metadata['response_evidence_status'] = 'write_failed'
            response.extensions['travel_evidence_write_error'] = type(error).__name__
        if not response.is_success:
            metadata['provider_error_body_status'] = 'write_failed' if write_error else 'complete'
        try:
            self.budget.settle(evidence['lease_id'],
                status='response_received' if response.is_success else 'http_error',
                usage=evidence['usage'], provider_request_id=evidence['provider_request_id'],
                elapsed_ms=evidence['elapsed_ms'], model=model, metadata=metadata,
                error=type(write_error).__name__ if write_error else None)
        except Exception as accounting_error:
            if response.is_success:
                raise
            response.extensions['travel_accounting_error'] = type(accounting_error).__name__
        if write_error is not None and response.is_success:
            raise write_error

    async def aclose(self):
        await self.inner.aclose()


class ComparisonModel:
    def __init__(self, budget, seed_path, *, case_id=None, api_key=None, transport_factory=None,
                 researcher=None):
        self.budget, self.seed_path = budget, Path(seed_path)
        self.api_key = api_key
        self.transport_factory = transport_factory
        self.case_id = case_id
        self.researcher = researcher

    def __call__(self, *, request_id, previous=None, feedback=None, answer_id=None):
        return asyncio.run(self.generate(request_id=request_id, previous=previous,
                                         feedback=feedback, answer_id=answer_id))

    async def generate(self, *, request_id, previous=None, feedback=None, answer_id=None):
        api_key = self.api_key or os.environ.get('DEEPSEEK_API_KEY')
        if not api_key:
            raise ValueError('DEEPSEEK_API_KEY is missing')
        seed = json.loads(self.seed_path.read_text(encoding='utf-8'))
        if 'cases' in seed:
            seed = next(case for case in seed['cases'] if case['id'] == self.case_id)
        if not isinstance(seed.get('brief'), str) or not seed['brief'].strip():
            raise ValueError('Explicit comparison brief is required')
        options = seed.get('options', [])
        if len(options) != 2 or len({option['id'] for option in options}) != 2:
            raise ValueError('Exactly two input options are required')
        if self.researcher is not None:
            seed = self.researcher(seed, request_id=request_id)
        seed_sources = {source['id']: source for source in seed.get('sources', [])}
        for source in seed_sources.values():
            if not isinstance(source.get('content'), str) or not source['content'].strip():
                raise ValueError('Source content is required')
        payload = {'confirmed_input': seed['brief'], 'options': options, 'sources': seed.get('sources', []),
                   'research_context': seed.get('research_context'),
                   'verification_context': prepare_verification_context(seed.get('verification_context')),
                   'previous_card': previous, 'explicit_feedback': feedback,
                   'answer_id': answer_id}
        prompt = json.dumps(payload, ensure_ascii=False)
        artifacts = self.budget.directory / 'model-artifacts'
        transport = RecordedTransport(self.budget, request_id, artifacts,
                                      self.transport_factory() if self.transport_factory else None)
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_SECONDS, follow_redirects=False) as http:
            client = AsyncOpenAI(api_key=api_key, base_url='https://api.deepseek.com',
                                 max_retries=0, http_client=http)
            provider = DeepSeekProvider(openai_client=client)
            model = build_strong_reasoning_model(provider)
            agent = Agent(model, output_type=boundary_output_type(ComparisonCard, request_id=request_id, artifacts=artifacts), retries=0,
                          system_prompt=SYSTEM_PROMPT,
                          model_settings=strong_reasoning_settings('comparison'))
            result = await agent.run(prompt, usage_limits=UsageLimits(request_limit=1))
        card = result.output
        expected_options = {option['id']: option['name'] for option in options}
        if {option.id: option.name for option in card.options} != expected_options:
            raise ValueError('Output options must match the supplied comparison case')
        for source in card.sources:
            expected = seed_sources.get(source.id)
            if expected is None or source.url != expected['url'] or source.title != expected['title']:
                raise ValueError('Model returned an unprovided source')
        private_json(artifacts / f'{request_id}-validated.json', {
            'card': card.model_dump(), 'model': MODEL,
            'pydantic_usage': str(result.usage)})
        return card.model_dump()
