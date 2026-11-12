"""LLM 基座（ADR-002）：叙事层的唯一入口。

纪律：
- 骨架层（solver/intent-matcher/graph/verify）禁止 import 本包——acceptance.py 有静态检查；
- LLM 只增色不承重：任何失败/超时/超限/无 key → 返回 None，调用方走模板兜底；
- ATOM_LLM_OFF=1 全局关闭（验收用）；
- 预算硬顶：每个 plan_id 的生成周期 LLM 调用 ≤ MAX_CALLS_PER_PLAN，超限自动降级；
- 意图指纹缓存：命中不重调（缓存命中不计调用数）。

配置：DEEPSEEK_API_KEY / OPENAI_BASE_URL 兼容；默认 https://api.deepseek.com/chat/completions，
model 默认 deepseek-chat（DEEPSEEK_MODEL 可覆盖）。
"""
from .client import (LLMOff, chat_text, calls_used, fingerprint,
                     load_cache, save_cache, MAX_CALLS_PER_PLAN, PROMPT_VERSION)

__all__ = ["LLMOff", "chat_text", "calls_used", "fingerprint",
           "load_cache", "save_cache", "MAX_CALLS_PER_PLAN", "PROMPT_VERSION"]
