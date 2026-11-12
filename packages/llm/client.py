"""LLM 统一客户端（stdlib urllib，零依赖）。详见包 docstring 的纪律清单。

返回约定：成功 → 文本 str；任何不可用 → None（绝不抛异常给调用方）。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request

PROMPT_VERSION = "v2"  # prompt 模板版本：改动模板必须 bump，否则旧缓存被误命中
                       # v2：chat 增 soft_constraint/advisor 路径 + 目的地 LLM 抽取
MAX_CALLS_PER_PLAN = 6  # ADR-002 §2.2：单次行程生成周期 LLM 调用预算硬顶

_CACHE_DIR = "data/cache/narrative"
_BACKOFF_S = (0.6, 1.8)  # 429/5xx 指数退避（最多 2 次重试）

_calls_by_plan: dict[str, int] = {}  # 进程内预算计数器（plan_id → 已用次数）


class LLMOff(Exception):
    """内部控制流：LLM 被关闭/预算耗尽（不向调用方传播，仅模块内使用）。"""


def _llm_disabled() -> bool:
    return os.environ.get("ATOM_LLM_OFF") == "1"


def calls_used(plan_id: str) -> int:
    return _calls_by_plan.get(plan_id, 0)


def fingerprint(*parts: object) -> str:
    """意图指纹：atom/意图关键字段 + prompt 版本 → 稳定哈希（缓存键）。"""
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def load_cache(fp: str) -> dict | None:
    """读叙事缓存（指纹全等才命中；损坏文件视为未命中）。"""
    if _llm_disabled():
        return None
    path = os.path.join(_CACHE_DIR, f"{fp}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def save_cache(fp: str, payload: dict) -> None:
    """原子写叙事缓存。"""
    os.makedirs(_CACHE_DIR, exist_ok=True)
    tmp = os.path.join(_CACHE_DIR, f"{fp}.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(_CACHE_DIR, f"{fp}.json"))


def chat_text(messages: list[dict], *, plan_id: str = "global",
              timeout: float = 20.0, max_tokens: int = 2000) -> str | None:
    """一次 chat 调用。成功返回 content 文本；任何失败/降级条件 → None。

    - ATOM_LLM_OFF=1 / 无 key → None（不尝试网络）
    - plan_id 预算超限（>MAX_CALLS_PER_PLAN）→ None（自动降级）
    - 429/5xx：指数退避最多重试 2 次；4xx（非 429）：立即失败
    - 缓存由调用方管理（fingerprint + load_cache/save_cache）
    """
    if _llm_disabled():
        return None
    if calls_used(plan_id) >= MAX_CALLS_PER_PLAN:
        return None
    api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    base_url = (os.environ.get("DEEPSEEK_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
                or "https://api.deepseek.com")
    model = os.environ.get("DEEPSEEK_MODEL") or os.environ.get("OPENAI_MODEL") or "deepseek-chat"

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    for attempt in range(3):  # 首次 + 2 次重试
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            _calls_by_plan[plan_id] = calls_used(plan_id) + 1  # 只计真实调用
            content = body["choices"][0]["message"]["content"]
            return content if isinstance(content, str) else None
        except urllib.error.HTTPError as e:
            if e.code == 429 or 500 <= e.code < 600:
                if attempt < 2:
                    time.sleep(_BACKOFF_S[attempt])
                    continue
            return None  # 4xx（非 429）立即失败；重试耗尽也失败
        except Exception:
            return None  # 网络/超时/解析异常一律降级，不抛给调用方
    return None
