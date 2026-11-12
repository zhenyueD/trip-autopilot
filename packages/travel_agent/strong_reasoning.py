"""Accepted DeepSeek strong-reasoning controls shared by travel capabilities."""
from __future__ import annotations

from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.deepseek import DeepSeekProvider
from .model_sizing import stage_limit

MODEL = 'deepseek-v4-pro'
REASONING_EFFORT = 'max'
TIMEOUT_SECONDS = 120


def build_strong_reasoning_model(provider: DeepSeekProvider) -> OpenAIChatModel:
    profile = {**(provider.model_profile(MODEL) or {}),
               'openai_chat_supports_max_completion_tokens': False,
               'openai_chat_supports_max_reasoning_effort': True,
               'openai_chat_thinking_false_body': {'thinking': {'type': 'disabled'}},
               'supports_forced_tool_choice_with_thinking': False}
    return OpenAIChatModel(MODEL, provider=provider, profile=profile)


def strong_reasoning_settings(stage='strong_reasoning') -> OpenAIChatModelSettings:
    # DeepSeek requires its explicit body toggle and native max_tokens field.
    return OpenAIChatModelSettings(
        thinking=REASONING_EFFORT, extra_body={'thinking': {'type': 'enabled'}},
        tool_choice='auto', top_p=0.95, max_tokens=stage_limit(stage), timeout=TIMEOUT_SECONDS)
