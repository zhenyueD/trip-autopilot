"""packages.intent 测试套件：python3 -m packages.intent.test_intent

纯 plain asserts，无第三方依赖。覆盖：
1. 六个中文输入用例（老人暖冬 / 情侣樱花 / 穷游特种兵 / 独行文化 / 亲子排除 / 模糊输入）
2. 所有解析产物的 schema 合法性
3. matcher 硬过滤确实排除违规原子
4. LLM sanitize 的字段级回退
5. 真实原子库（若存在）的集成冒烟
"""
from __future__ import annotations

import os

try:
    from . import matcher, parser_heuristic, schema
    from .parser_llm import sanitize_llm_vector
except ImportError:  # 允许 python3 packages/intent/test_intent.py 直接运行（首选 -m 跑法）
    import sys
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    from packages.intent import matcher, parser_heuristic, schema
    from packages.intent.parser_llm import sanitize_llm_vector

_FIXTURE = os.path.join(os.path.dirname(__file__), "data", "sample_atoms.json")
_REAL_OSAKA = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "atoms", "osaka.json"))

_PASSED = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASSED
    assert cond, f"FAIL [{label}] {detail}"
    _PASSED += 1
    print(f"  ok  {label}")


def assert_valid(vec: dict, label: str) -> None:
    violations = schema.validate_intent_vector(vec)
    check(f"{label}: schema 合法", not violations, str(violations))


# ---------------- 六个输入用例 ----------------

def case_1_elderly_winter_warmth():
    vec = parser_heuristic.parse("过年想带爸妈去个不累暖和的地方，大概5天")
    assert_valid(vec, "case1")
    check("case1: elderly_parents", "elderly_parents" in vec["party"]["composition"])
    check("case1: mobility ≤ 2", vec["party"]["mobility_max_physical_level"] <= 2)
    check("case1: mobility 进 inferred", "party.mobility_max_physical_level" in vec["inferred"])
    check("case1: winter", vec["time"]["season"] == "winter")
    check("case1: 过年日期区间推断",
          vec["time"]["departure_date_range"] is not None
          and "time.departure_date_range" in vec["inferred"])
    check("case1: duration 5", vec["time"]["duration_days"] == 5)
    check("case1: 暖和 → min_day_temp_c ≥ 15",
          vec["geography"]["climate_wish"] == {"min_day_temp_c": 15})
    check("case1: 不累 → pace ≤ 3", vec["pace"]["atoms_per_day"] <= 3)
    check("case1: 不累 → strenuous 排除", "strenuous_hiking" in vec["explicit_exclusions"])
    check("case1: 目的地缺失进 uncertainty",
          any(u["field"] == "geography.destination_specified" for u in vec["uncertainty"]))
    check("case1: 预算缺失进 uncertainty",
          any(u["field"] == "budget.per_person_cny" for u in vec["uncertainty"]))
    check("case1: confidence > 0.5", vec["confidence"] > 0.5, str(vec["confidence"]))


def case_2_couple_sakura_osaka():
    vec = parser_heuristic.parse("想和男朋友樱花季去大阪拍照吃美食，4天")
    assert_valid(vec, "case2")
    check("case2: couple", "couple" in vec["party"]["composition"])
    check("case2: spring", vec["time"]["season"] == "spring")
    check("case2: 樱花季日期区间", vec["time"]["departure_date_range"] is not None)
    check("case2: osaka", vec["geography"]["destination_specified"] == "osaka")
    check("case2: photo + foodie",
          "photo" in vec["emotion_goals"] and "foodie" in vec["emotion_goals"])
    check("case2: duration 4", vec["time"]["duration_days"] == 4)


def case_3_shoestring_bangkok_commando():
    vec = parser_heuristic.parse("穷游曼谷，特种兵打卡，预算人均3000")
    assert_valid(vec, "case3")
    check("case3: bangkok", vec["geography"]["destination_specified"] == "bangkok")
    check("case3: 穷游 tier ≤ 2", vec["budget"]["tier_preference"] <= 2)
    check("case3: per_person 3000", vec["budget"]["per_person_cny"] == 3000)
    check("case3: 特种兵 atoms_per_day ≥ 4", vec["pace"]["atoms_per_day"] >= 4)
    check("case3: 特种兵 early_start_ok", vec["pace"]["early_start_ok"] is True)
    check("case3: 打卡 → iconic", "iconic" in vec["emotion_goals"])


def case_4_solo_culture_deep():
    vec = parser_heuristic.parse("一个人想去京都深度体验文化和历史，慢慢逛")
    assert_valid(vec, "case4")
    check("case4: solo", "solo" in vec["party"]["composition"])
    check("case4: size 1", vec["party"]["size"] == 1)
    check("case4: kyoto", vec["geography"]["destination_specified"] == "kyoto")
    check("case4: cultural_deep", "cultural_deep" in vec["emotion_goals"])
    check("case4: 慢慢逛 → pace ≤ 3", vec["pace"]["atoms_per_day"] <= 3)


def case_5_family_kids_no_strenuous():
    vec = parser_heuristic.parse("带孩子一家三口去大阪玩3天，不要爬山不要太累")
    assert_valid(vec, "case5")
    check("case5: family_kids", "family_kids" in vec["party"]["composition"])
    check("case5: size 3", vec["party"]["size"] == 3)
    check("case5: duration 3", vec["time"]["duration_days"] == 3)
    check("case5: osaka", vec["geography"]["destination_specified"] == "osaka")
    check("case5: strenuous 排除", "strenuous_hiking" in vec["explicit_exclusions"])
    check("case5: mobility ≤ 3", vec["party"]["mobility_max_physical_level"] <= 3)


def case_6_vague_low_confidence():
    vec = parser_heuristic.parse("想出去走走")
    assert_valid(vec, "case6")
    check("case6: confidence < 0.5 触发澄清", vec["confidence"] < 0.5, str(vec["confidence"]))
    check("case6: uncertainty ≥ 2", len(vec["uncertainty"]) >= 2)
    check("case6: uncertainty ≤ 3 问（spec §1）", len(vec["uncertainty"]) <= 3)
    check("case6: 每问 ≤ 4 选项", all(len(u["options"]) <= 4 for u in vec["uncertainty"]))
    fields = {u["field"] for u in vec["uncertainty"]}
    check("case6: 覆盖目的地/日期/预算",
          {"geography.destination_specified", "time.departure_date_range", "budget.per_person_cny"} <= fields)


# ---------------- matcher 硬过滤 ----------------

def case_matcher_hard_filters():
    atoms = matcher.load_atoms(_FIXTURE)
    usj = "osaka.osaka_bay.universal_studios_japan"
    crab = "osaka.dotonbori.kanidoraku_honten"

    # 老人意图：USJ（physical 4 > mobility 2，且 unsuitable_for elderly）必须被排除
    elderly = parser_heuristic.parse("过年想带爸妈去大阪，不累，5天")
    plan = matcher.build_plan(elderly, atoms)
    excluded_ids = {e["atom_id"]: e["reason"] for e in plan["excluded_atoms"]}
    check("matcher: 老人排除 USJ", usj in excluded_ids)
    check("matcher: USJ 排除原因含体力/适配",
          "physical_level" in excluded_ids.get(usj, "") or "unsuitable_for" in excluded_ids.get(usj, ""))

    # hard_cap 预算：tier 4 的蟹道乐必须被排除
    tight = parser_heuristic.parse("预算人均3000不超过，去大阪穷游3天")
    check("matcher: hard_cap 识别", tight["budget"]["hard_cap"] is True)
    plan2 = matcher.build_plan(tight, atoms)
    excluded2 = {e["atom_id"]: e["reason"] for e in plan2["excluded_atoms"]}
    check("matcher: hard_cap 排除 tier4 蟹道乐",
          crab in excluded2 and excluded2[crab].startswith("budget_tier"))
    check("matcher: hard_cap 排除 tier4 USJ", usj in excluded2)

    # 非 hard_cap 放宽一档：tier_preference 3 → 允许 tier 4
    comfy = parser_heuristic.parse("和男朋友去大阪4天，拍照吃美食")
    check("matcher: 非 hard_cap", comfy["budget"]["hard_cap"] is False)
    plan3 = matcher.build_plan(comfy, atoms)
    excluded3 = {e["atom_id"] for e in plan3["excluded_atoms"]}
    check("matcher: 放宽一档后蟹道乐可入选", crab not in excluded3)

    # 目的地指定时排除其他目的地原子
    bangkok = parser_heuristic.parse("穷游曼谷，特种兵打卡，预算人均3000")
    plan4 = matcher.build_plan(bangkok, atoms)
    check("matcher: 曼谷意图排除全部大阪原子",
          len(plan4["excluded_atoms"]) == len(atoms)
          and all(e["reason"].startswith("destination_mismatch") for e in plan4["excluded_atoms"]))

    # 方案结构：每天 1 个 cluster，槽位有序，理由占位与风险旗标存在
    plan5 = matcher.build_plan(comfy, atoms)
    check("matcher: duration_days=4", plan5["duration_days"] == 4)
    check("matcher: 每天绑定 1 个 cluster",
          all(isinstance(d["cluster"], str) or d["cluster"] is None for d in plan5["days"]))
    for d in plan5["days"]:
        for item in d["items"]:
            check(f"matcher: {item['atom_id']} 有槽位", item["slot"] in matcher.SLOT_ORDER)
            check(f"matcher: {item['atom_id']} 理由占位", "占位" in item["rationale"])
            check(f"matcher: {item['atom_id']} 风险旗标为数组", isinstance(item["risk_flags"], list))


def case_booking_risk_flags():
    """booking_required=required 且出发在 lead_days 内 → booking_urgent 预警。"""
    atoms = matcher.load_atoms(_FIXTURE)
    # 默认意图：mobility 5、非 hard_cap → USJ 入选；出发日期很近 → 触发 urgent
    vec = schema.default_intent_vector("下周去大阪玩环球影城")
    vec["geography"]["destination_specified"] = "osaka"
    vec["time"]["departure_date_range"] = ["2026-10-01", "2026-10-05"]
    vec["emotion_goals"] = ["family_fun", "adventure"]
    plan = matcher.build_plan(vec, atoms, today=__import__("datetime").date(2026, 9, 28))
    usj_items = [i for d in plan["days"] for i in d["items"]
                 if i["atom_id"] == "osaka.osaka_bay.universal_studios_japan"]
    check("matcher: USJ 入选", len(usj_items) == 1)
    check("matcher: booking_urgent 触发",
          any(f.startswith("booking_urgent") for f in usj_items[0]["risk_flags"]))


# ---------------- LLM sanitize ----------------

def case_llm_sanitize():
    base = parser_heuristic.parse("过年想带爸妈去个不累暖和的地方，大概5天")
    raw = {
        "party": {"composition": ["elderly_parents"], "size": 3, "mobility_max_physical_level": 2},
        "time": {"season": "winter", "duration_days": "五天", "departure_date_range": None, "flexible": False},  # 非法：duration_days 是字符串
        "budget": {"tier_preference": 99},  # 非法：越界
        "emotion_goals": ["healing", "hacking"],  # 非法枚举
        "geography": {"destination_specified": "okinawa", "climate_wish": {"min_day_temp_c": 18},
                      "origin": None, "max_flight_hours": None},
        "confidence": 1.0,  # 合法但会被封顶 0.9
        "schema_version": "9.9",  # 不可覆盖
        "raw_input": "篡改",      # 不可覆盖
    }
    vec, dropped = sanitize_llm_vector(raw, "过年想带爸妈去个不累暖和的地方，大概5天", base=base)
    assert_valid(vec, "sanitize")
    check("sanitize: 非法 time 字段被丢弃", "time" in dropped)
    check("sanitize: 非法 budget 字段被丢弃", "budget" in dropped)
    check("sanitize: 非法 emotion_goals 被丢弃", "emotion_goals" in dropped)
    check("sanitize: 合法 party 字段被采纳", vec["party"]["size"] == 3)
    check("sanitize: 合法 geography 字段被采纳",
          vec["geography"]["destination_specified"] == "okinawa")
    check("sanitize: confidence 封顶 0.9", vec["confidence"] == 0.9)
    check("sanitize: schema_version 不可篡改", vec["schema_version"] == schema.SCHEMA_VERSION)
    check("sanitize: raw_input 不可篡改", vec["raw_input"] == "过年想带爸妈去个不累暖和的地方，大概5天")
    check("sanitize: 回退后 time 仍是启发式结果", vec["time"]["duration_days"] == 5)

    # 整个输出非 dict → 全量回退
    vec2, dropped2 = sanitize_llm_vector([1, 2, 3], "想出去走走", base=None)
    assert_valid(vec2, "sanitize-非dict")
    check("sanitize: 非 dict 输出全量回退", bool(dropped2))


# ---------------- 集成冒烟 ----------------

def case_real_atoms_smoke():
    if not os.path.exists(_REAL_OSAKA):
        print(f"  --  [smoke] {_REAL_OSAKA} 不存在，跳过真实原子库冒烟（不失败）")
        return
    atoms = matcher.load_atoms(_REAL_OSAKA)
    vec = parser_heuristic.parse("和男朋友樱花季去大阪拍照吃美食，4天")
    plan = matcher.build_plan(vec, atoms)
    check("smoke: 真实原子库产出方案", plan["duration_days"] == 4 and len(plan["days"]) == 4)
    print(f"  --  [smoke] 真实原子库 {len(atoms)} 原子 → 排除 {len(plan['excluded_atoms'])} 项")


def main() -> int:
    global _PASSED
    cases = [
        case_1_elderly_winter_warmth,
        case_2_couple_sakura_osaka,
        case_3_shoestring_bangkok_commando,
        case_4_solo_culture_deep,
        case_5_family_kids_no_strenuous,
        case_6_vague_low_confidence,
        case_matcher_hard_filters,
        case_booking_risk_flags,
        case_llm_sanitize,
        case_real_atoms_smoke,
    ]
    for case in cases:
        print(f"\n[{case.__name__}]")
        case()
    print(f"\n全部通过：{_PASSED} 项断言 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
