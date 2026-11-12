"""可插拔 LLM 解析后端。

spec §6 边界：
- LLM 只做"理解"（文本 → 向量）；
- LLM 输出的每个字段必须过 schema 校验，非法值丢弃回退默认，不许直通下游；
- 无 LLM 可用时启发式解析器独立可用（NullBackend 显式表达"不可用"）。

key 纪律：只从环境变量读 OPENAI_API_KEY / OPENAI_BASE_URL，代码里不硬编码任何密钥；
未配置时后端明确抛 LLMUnavailableError，调用方回退启发式。
"""
from __future__ import annotations

import copy
import json
import os
import urllib.request
from typing import Optional

from .schema import SCHEMA_VERSION, default_intent_vector, validate_intent_vector


class LLMUnavailableError(Exception):
    """LLM 后端不可用（无 key / 网络失败 / 输出不可解析）。调用方应回退启发式。"""


class LLMBackend:
    """LLM 解析后端接口：parse(text) -> dict（IntentVector 候选 JSON）。"""

    def parse(self, text: str) -> dict:
        raise NotImplementedError


class NullBackend(LLMBackend):
    """显式的"无 LLM"后端：总是抛不可用，用于 demo / 降级模式。"""

    def parse(self, text: str) -> dict:
        raise LLMUnavailableError("NullBackend：未配置 LLM，走启发式解析")


# LLM 允许覆盖的顶层字段。schema_version / raw_input 永远由本模块控制，不给模型。
_SANITIZABLE_KEYS = [
    "party", "time", "budget", "geography", "pace",
    "emotion_goals", "hard_constraints", "explicit_interests",
    "explicit_exclusions", "uncertainty", "confidence", "inferred",
]

# LLM 自报置信度上限：模型的自我评估不可全信，封顶 0.9。
_LLM_CONFIDENCE_CAP = 0.9


def build_prompt(text: str) -> list[dict]:
    """要求模型输出严格符合 IntentVector schema 的 JSON。"""
    system = (
        "你是旅行意图解析器。把用户的中文旅行需求解析为一个 IntentVector JSON 对象。\n"
        f"schema_version 固定为 \"{SCHEMA_VERSION}\"。字段与规则：\n"
        "- party: {composition: enum[solo,couple,elderly_parents,family_kids,group_friends,business] 数组, "
        "size: 整数|null, mobility_max_physical_level: 1-5 整数（带老人≤2，带小孩≤3，不确定给5）}\n"
        "- time: {departure_date_range: [\"YYYY-MM-DD\",\"YYYY-MM-DD\"]|null, duration_days: 整数|null, "
        "season: spring|summer|autumn|winter|null, flexible: bool}\n"
        "- budget: {total_cny: 数字|null, per_person_cny: 数字|null, tier_preference: 1-4 整数, hard_cap: bool}\n"
        "- geography: {origin: 字符串|null, destination_specified: 字符串|null, "
        "climate_wish: {min_day_temp_c: 数字}|{max_day_temp_c: 数字}|null, max_flight_hours: 数字|null}\n"
        "- pace: {atoms_per_day: 1-6 整数（默认3，特种兵4-5，老人2-3）, early_start_ok: bool, rest_afternoon: bool}\n"
        "- emotion_goals: 从 [healing,lively_local,photo,foodie,romantic,family_fun,nostalgic,luxury,"
        "adventure,quiet_escape,cultural_deep,night_view,bargain,iconic,hidden_gem] 中选\n"
        "- hard_constraints / explicit_interests / explicit_exclusions: 字符串数组\n"
        "- uncertainty: [{field, question, options(≤4)}]，关键缺失字段（目的地/日期/预算）各补一句自然语言问题，最多3条\n"
        "- confidence: 0-1 数字；inferred: 推断字段的字符串数组\n"
        "纪律：未知就 null，宁可空缺不可编造；只输出 JSON 对象本身，不要 markdown 代码围栏，不要解释。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]


class OpenAICompatibleBackend(LLMBackend):
    """OpenAI 兼容 Chat Completions 后端（stdlib urllib，零依赖）。"""

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    def parse(self, text: str) -> dict:
        api_key = os.environ.get("OPENAI_API_KEY")
        base_url = os.environ.get("OPENAI_BASE_URL")
        if not api_key or not base_url:
            raise LLMUnavailableError("未设置 OPENAI_API_KEY / OPENAI_BASE_URL 环境变量")
        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

        payload = {
            "model": model,
            "messages": build_prompt(text),
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # 网络 / HTTP / 解析失败统一视为不可用
            raise LLMUnavailableError(f"LLM 请求失败：{e}") from e

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMUnavailableError(f"LLM 响应结构异常：{e}") from e

        content = content.strip()
        if content.startswith("```"):  # 容忍模型包 markdown 围栏
            content = content.strip("`")
            if content.startswith("json"):
                content = content[4:]
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as e:
            raise LLMUnavailableError(f"LLM 输出不是合法 JSON：{e}") from e
        if not isinstance(parsed, dict):
            raise LLMUnavailableError("LLM 输出不是 JSON object")
        return parsed


def sanitize_llm_vector(raw: object, raw_input: str, base: Optional[dict] = None) -> tuple[dict, list[str]]:
    """spec §6 的核心安全闸：LLM 输出逐字段过 schema 校验，非法字段丢弃回退默认。

    策略（字段级回退，不是整体回退）：
    1. 以 base（通常是启发式结果；缺省为全默认向量）为底；
    2. schema_version / raw_input 由本模块强制覆写，永不信任模型；
    3. 逐个顶层字段套用 LLM 值，套完后整体验证——任何违规即回退该字段到底线值，
       并记入 dropped 供审计；
    4. LLM 自报 confidence 封顶 0.9（模型自我评估不可全信）。

    返回 (合法 IntentVector, 被丢弃的字段名列表)。
    """
    vec = copy.deepcopy(base) if base is not None else default_intent_vector(raw_input)
    vec["schema_version"] = SCHEMA_VERSION
    vec["raw_input"] = raw_input

    dropped: list[str] = []
    if not isinstance(raw, dict):
        return vec, ["<整个 LLM 输出非 object>"]

    for key in _SANITIZABLE_KEYS:
        if key not in raw:
            continue
        snapshot = copy.deepcopy(vec.get(key))
        vec[key] = copy.deepcopy(raw[key])
        if validate_intent_vector(vec):
            vec[key] = snapshot
            dropped.append(key)

    conf = vec.get("confidence")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool):
        vec["confidence"] = round(min(float(conf), _LLM_CONFIDENCE_CAP), 2)

    # 兜底：无论如何返回值必须过校验（base 自身合法时这里恒为空）。
    # 显式 raise，不用 assert——python -O 下 assert 被剥离，安全闸不能失效。
    violations = validate_intent_vector(vec)
    if violations:
        raise RuntimeError(f"sanitize 产物仍非法（内部不变量破坏）：{violations}")
    return vec, dropped


# ---- 目的地去词典化（ADR-002 v2）----
#
# 词典（parser_heuristic.DESTINATIONS）保留为快速路径；未命中时走这里：
# LLM 只负责"抽出城市名"这一件事，注册表匹配/回落话术全在 server 的确定性层。
# 走 packages.llm 统一基座（DEEPSEEK key、退避重试、每 plan ≤6 次预算硬顶），
# LLM 不可用 → None → server 给"没听出目的地"的诚实话术，绝不静默回落。

_DEST_EXTRACT_SCHEMA = (
    "你是目的地抽取器。从用户的中文旅行需求里抽取想去的城市。"
    "输出严格 JSON：{\"city_zh\": \"城市中文名\", \"city_en\": \"英文惯用名或 null\"}；"
    "没有明确提到城市（只说季节/偏好/预算等）就输出 {\"city_zh\": null, \"city_en\": null}。"
    "只输出 JSON，不要解释。"
)


def extract_destination_llm(text: str, *, plan_id: str = "global") -> dict | None:
    """词典未命中时的 LLM 城市抽取。

    返回 {"city_zh": str, "city_en": str|None}；无明确城市 / LLM 不可用 /
    输出非法 → None（调用方按"未识别"走诚实话术）。
    """
    from packages.llm import chat_text  # 延迟 import：保持本模块无 LLM 也可被骨架层引用
    raw = chat_text(
        [{"role": "system", "content": _DEST_EXTRACT_SCHEMA},
         {"role": "user", "content": text}],
        plan_id=plan_id, timeout=15.0, max_tokens=120)
    if raw is None:
        return None
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    city_zh = data.get("city_zh")
    if not isinstance(city_zh, str) or not city_zh.strip():
        return None
    city_en = data.get("city_en")
    return {"city_zh": city_zh.strip(),
            "city_en": city_en.strip() if isinstance(city_en, str) and city_en.strip() else None}
