"""IntentVector 结构定义与运行时校验。

代码合同：docs/INTENT-ATOM-SCHEMA.md §2/§3（版本 0.1）。
schema 即合同：字段名、枚举、类型以此文件与文档双向对齐；改动必须先改文档并 bump 版本。

这里用 TypedDict 做静态结构声明（文档化 + IDE 提示），
运行时一律以普通 dict 流转（LLM/启发式输出都是 JSON 对象），
由 validate_intent_vector() 做唯一的确定性校验入口。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional, TypedDict

SCHEMA_VERSION = "0.2"

# ---- 枚举表（与 docs/INTENT-ATOM-SCHEMA.md §3、docs/SUPPLY-ATOM-SCHEMA.md §4.2 对齐）----

PARTY_COMPOSITIONS = [
    "solo", "couple", "elderly_parents", "family_kids", "group_friends", "business",
]

SEASONS = ["spring", "summer", "autumn", "winter"]

# emotion_goals 枚举同供给 schema §4.2
EMOTION_TAGS = [
    "healing", "lively_local", "photo", "foodie", "romantic", "family_fun",
    "nostalgic", "luxury", "adventure", "quiet_escape", "cultural_deep",
    "night_view", "bargain", "iconic", "hidden_gem",
]


# ---- TypedDict 结构声明（仅静态提示，运行时靠 validate） ----

class Party(TypedDict, total=False):
    composition: list[str]
    size: Optional[int]
    mobility_max_physical_level: int  # 1-5，与原子 physical_level 做硬过滤


class Time(TypedDict, total=False):
    departure_date_range: Optional[list[str]]  # ["YYYY-MM-DD", "YYYY-MM-DD"] 或 null
    duration_days: Optional[int]
    season: Optional[str]
    flexible: bool


class Budget(TypedDict, total=False):
    total_cny: Optional[float]
    per_person_cny: Optional[float]
    tier_preference: int  # 1-4
    hard_cap: bool


class Geography(TypedDict, total=False):
    origin: Optional[str]
    destination_specified: Optional[Any]  # null | str | list[str]
    climate_wish: Optional[dict]          # 结构化气候诉求，如 {"min_day_temp_c": 15}
    max_flight_hours: Optional[float]


class Pace(TypedDict, total=False):
    atoms_per_day: int        # 默认 3（舒适），特种兵 4-5，老人 2-3
    early_start_ok: bool
    rest_afternoon: bool


class UncertaintyItem(TypedDict):
    field: str
    question: str
    options: list[str]  # ≤ 4 个可点选项


# IntentVector：docs/INTENT-ATOM-SCHEMA.md §2 的镜像。
IntentVector = TypedDict("IntentVector", {
    "schema_version": str,
    "raw_input": str,
    "party": Party,
    "time": Time,
    "budget": Budget,
    "geography": Geography,
    "pace": Pace,
    "emotion_goals": list[str],
    "hard_constraints": list[str],
    "explicit_interests": list[str],
    "explicit_exclusions": list[str],
    "explicit_pois": list[str],   # v0.2 新增：意图中点名 POI 锚定的 atom_id 列表
    "uncertainty": list[UncertaintyItem],
    "confidence": float,
    "inferred": list[str],
}, total=False)


def default_intent_vector(raw_input: str = "") -> dict:
    """草稿先行的默认向量：所有未知字段为 null/默认值，由解析器逐步覆盖。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "raw_input": raw_input,
        "party": {
            "composition": [],
            "size": None,
            "mobility_max_physical_level": 5,  # 默认不限制体力
        },
        "time": {
            "departure_date_range": None,
            "duration_days": None,
            "season": None,
            "flexible": True,
        },
        "budget": {
            "total_cny": None,
            "per_person_cny": None,
            "tier_preference": 3,
            "hard_cap": False,
        },
        "geography": {
            "origin": None,
            "destination_specified": None,
            "climate_wish": None,
            "max_flight_hours": None,
        },
        "pace": {
            "atoms_per_day": 3,
            "early_start_ok": False,
            "rest_afternoon": False,
        },
        "emotion_goals": [],
        "hard_constraints": [],
        "explicit_interests": [],
        "explicit_exclusions": [],
        "explicit_pois": [],
        "uncertainty": [],
        "confidence": 0.0,
        "inferred": [],
    }


# ---- 校验 ----

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _is_str_list(x: Any) -> bool:
    return isinstance(x, list) and all(isinstance(i, str) for i in x)


def _check_date(s: Any) -> bool:
    if not isinstance(s, str) or not _DATE_RE.match(s):
        return False
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def validate_intent_vector(v: Any) -> list[str]:
    """确定性校验：类型 / 枚举 / 范围。返回违规描述列表，空列表 = 合法。

    这是 LLM 输出、启发式输出、跨模块传递的唯一校验入口（spec §6）。
    """
    violations: list[str] = []

    if not isinstance(v, dict):
        return ["root: 必须是 JSON object"]

    if v.get("schema_version") != SCHEMA_VERSION:
        violations.append(f"schema_version: 必须为 \"{SCHEMA_VERSION}\"，得到 {v.get('schema_version')!r}")
    if not isinstance(v.get("raw_input"), str):
        violations.append("raw_input: 必须是字符串（原文永久保留）")

    # party
    party = v.get("party")
    if not isinstance(party, dict):
        violations.append("party: 必须是 object")
    else:
        comp = party.get("composition")
        if not _is_str_list(comp):
            violations.append("party.composition: 必须是字符串数组")
        else:
            bad = [c for c in comp if c not in PARTY_COMPOSITIONS]
            if bad:
                violations.append(f"party.composition: 非法枚举值 {bad}，允许 {PARTY_COMPOSITIONS}")
        size = party.get("size")
        if size is not None and not (isinstance(size, int) and not isinstance(size, bool) and size >= 1):
            violations.append("party.size: 必须是 ≥1 的整数或 null")
        mob = party.get("mobility_max_physical_level")
        if not (isinstance(mob, int) and not isinstance(mob, bool) and 1 <= mob <= 5):
            violations.append("party.mobility_max_physical_level: 必须是 1-5 的整数")

    # time
    time_ = v.get("time")
    if not isinstance(time_, dict):
        violations.append("time: 必须是 object")
    else:
        rng = time_.get("departure_date_range")
        if rng is not None:
            if not (isinstance(rng, list) and len(rng) == 2 and all(_check_date(d) for d in rng)):
                violations.append("time.departure_date_range: 必须是 [\"YYYY-MM-DD\",\"YYYY-MM-DD\"] 或 null")
            elif rng[0] > rng[1]:
                violations.append("time.departure_date_range: 起始日期不能晚于结束日期")
        dur = time_.get("duration_days")
        if dur is not None and not (isinstance(dur, int) and not isinstance(dur, bool) and 1 <= dur <= 60):
            violations.append("time.duration_days: 必须是 1-60 的整数或 null")
        season = time_.get("season")
        if season is not None and season not in SEASONS:
            violations.append(f"time.season: 必须是 {SEASONS} 之一或 null，得到 {season!r}")
        if not isinstance(time_.get("flexible"), bool):
            violations.append("time.flexible: 必须是 bool")

    # budget
    budget = v.get("budget")
    if not isinstance(budget, dict):
        violations.append("budget: 必须是 object")
    else:
        for key in ("total_cny", "per_person_cny"):
            val = budget.get(key)
            if val is not None and not (_is_number(val) and val >= 0):
                violations.append(f"budget.{key}: 必须是非负数字或 null")
        tier = budget.get("tier_preference")
        if not (isinstance(tier, int) and not isinstance(tier, bool) and 1 <= tier <= 4):
            violations.append("budget.tier_preference: 必须是 1-4 的整数")
        if not isinstance(budget.get("hard_cap"), bool):
            violations.append("budget.hard_cap: 必须是 bool")

    # geography
    geo = v.get("geography")
    if not isinstance(geo, dict):
        violations.append("geography: 必须是 object")
    else:
        if geo.get("origin") is not None and not isinstance(geo.get("origin"), str):
            violations.append("geography.origin: 必须是字符串或 null")
        dest = geo.get("destination_specified")
        if dest is not None:
            if isinstance(dest, str):
                pass
            elif _is_str_list(dest) and len(dest) >= 1:
                pass
            else:
                violations.append("geography.destination_specified: 必须是非空字符串、字符串数组或 null")
        cw = geo.get("climate_wish")
        if cw is not None:
            if not isinstance(cw, dict) or not all(isinstance(k, str) and _is_number(val) for k, val in cw.items()):
                violations.append("geography.climate_wish: 必须是 {字符串键: 数字} 的 object 或 null")
        mfh = geo.get("max_flight_hours")
        if mfh is not None and not (_is_number(mfh) and mfh > 0):
            violations.append("geography.max_flight_hours: 必须是正数或 null")

    # pace
    pace = v.get("pace")
    if not isinstance(pace, dict):
        violations.append("pace: 必须是 object")
    else:
        apd = pace.get("atoms_per_day")
        if not (isinstance(apd, int) and not isinstance(apd, bool) and 1 <= apd <= 6):
            violations.append("pace.atoms_per_day: 必须是 1-6 的整数")
        for key in ("early_start_ok", "rest_afternoon"):
            if not isinstance(pace.get(key), bool):
                violations.append(f"pace.{key}: 必须是 bool")

    # emotion_goals
    eg = v.get("emotion_goals")
    if not _is_str_list(eg):
        violations.append("emotion_goals: 必须是字符串数组")
    else:
        bad = [t for t in eg if t not in EMOTION_TAGS]
        if bad:
            violations.append(f"emotion_goals: 非法枚举值 {bad}，允许 {EMOTION_TAGS}")

    for key in ("hard_constraints", "explicit_interests", "explicit_exclusions",
                "explicit_pois", "inferred"):
        if not _is_str_list(v.get(key)):
            violations.append(f"{key}: 必须是字符串数组")

    # uncertainty
    unc = v.get("uncertainty")
    if not isinstance(unc, list):
        violations.append("uncertainty: 必须是数组")
    else:
        for i, item in enumerate(unc):
            if not isinstance(item, dict):
                violations.append(f"uncertainty[{i}]: 必须是 object")
                continue
            if not isinstance(item.get("field"), str) or not item["field"]:
                violations.append(f"uncertainty[{i}].field: 必须是非空字符串")
            if not isinstance(item.get("question"), str) or not item["question"]:
                violations.append(f"uncertainty[{i}].question: 必须是非空字符串")
            opts = item.get("options")
            if not (_is_str_list(opts) and 1 <= len(opts) <= 4):
                violations.append(f"uncertainty[{i}].options: 必须是 1-4 个字符串")

    conf = v.get("confidence")
    if not (_is_number(conf) and 0.0 <= conf <= 1.0):
        violations.append("confidence: 必须是 0-1 的数字")

    return violations
