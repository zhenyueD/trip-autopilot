"""确定性匹配引擎 —— docs/INTENT-ATOM-SCHEMA.md §4 匹配契约的实现。

铁律（spec §6）：匹配 / 排序 / 求解全部是确定性代码，LLM 永不参与。
输入：IntentVector + 原子列表（schema 见 docs/SUPPLY-ATOM-SCHEMA.md）；
输出：结构化方案草稿 JSON（days → cluster → 时间槽 → atom ids + 理由占位 + 风险旗标）。
"""
from __future__ import annotations

import json
from datetime import date
from typing import Optional

from .weights import RELAXED_ATOMS_PER_DAY, WEIGHTS

# IntentVector party.composition → 原子 suitable_for / unsuitable_for 的标签映射
PARTY_TO_ATOM_TAGS = {
    "solo": ["solo"],
    "couple": ["couple"],
    "elderly_parents": ["elderly"],
    "family_kids": ["family", "kids"],
    "group_friends": ["group_friends"],
    "business": ["business"],
}

# explicit_exclusions 自由字符串 → 确定性排除谓词（spec §3：逐条转确定性校验规则）
EXCLUSION_PREDICATES = {
    "strenuous_hiking": lambda atom: atom.get("constraints", {}).get("physical_level", 1) >= 4,
    # ADR-002 §2.3：chat "avoid_outdoor" 的落点——纯户外原子硬排除
    "outdoor_only": lambda atom: atom.get("time_structure", {}).get("weather_dependency") == "outdoor_only",
}

# chat soft_constraint（ADR-002 §2.3 v2）：LLM 只产出语义标签，匹配/加权全在这里。
# 行为标签 → 确定性谓词；情绪/类别标签与 atom.emotion_tags / categories 直接比对；
# 词表外的标签不命中（安全：LLM 编不出匹配规则，只能选标签）。
SOFT_TAG_PREDICATES = {
    "queue": lambda atom: atom.get("constraints", {}).get("crowd_level", 3) >= 4,
    "crowded": lambda atom: atom.get("constraints", {}).get("crowd_level", 3) >= 4,
    "indoor": lambda atom: atom.get("time_structure", {}).get("weather_dependency") == "indoor_ok",
    "outdoor": lambda atom: atom.get("time_structure", {}).get("weather_dependency") == "outdoor_preferred",
}


def soft_tag_hits(tags: list, atom: dict) -> int:
    """软标签命中数（确定性）。tags 来自 chat soft_constraint 的 schema 校验产物。"""
    hits = 0
    etags = set(atom.get("emotion_tags", []))
    cats = set(atom.get("categories", []))
    for t in tags:
        if not isinstance(t, str):
            continue
        if t in etags or t in cats:
            hits += 1
            continue
        pred = SOFT_TAG_PREDICATES.get(t)
        if pred is not None and pred(atom):
            hits += 1
    return hits


# 时间槽顺序：早 → 夜（spec §4 第 4 条）
SLOT_ORDER = ["morning", "noon", "afternoon", "sunset", "evening", "night"]

BEST_TIME_TO_SLOT = {
    "early_morning": "morning",
    "morning": "morning",
    "noon": "noon",
    "afternoon": "afternoon",
    "sunset": "sunset",
    "evening": "evening",
    "night": "night",
    "any": None,  # 任意时段：填最早空槽
}

# 事实新鲜度阈值（spec §3.7：超过 180 天未复核的原子降级为"待核实"）
STALE_FACT_DAYS = 180

DEFAULT_DURATION_DAYS = 3


def load_atoms(path: str) -> list[dict]:
    """加载原子库 JSON。兼容两种形态：裸数组，或带元信息的 {"atoms": [...]} 对象。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        atoms = data.get("atoms", [])
    else:
        atoms = data
    if not isinstance(atoms, list):
        raise ValueError(f"{path}: 原子库必须是数组或含 atoms 数组的 object")
    return atoms


def _destinations_of(intent: dict) -> Optional[set]:
    dest = intent.get("geography", {}).get("destination_specified")
    if dest is None:
        return None
    return {dest} if isinstance(dest, str) else set(dest)


def hard_filter_reason(intent: dict, atom: dict) -> Optional[str]:
    """spec §4 第 1 条硬过滤。返回排除原因；None = 通过。"""
    constraints = atom.get("constraints", {})

    dests = _destinations_of(intent)
    if dests is not None and atom.get("destination") not in dests:
        return f"destination_mismatch:{atom.get('destination')}"

    mobility = intent.get("party", {}).get("mobility_max_physical_level", 5)
    if constraints.get("physical_level", 1) > mobility:
        return f"physical_level:{constraints.get('physical_level')}>mobility_max:{mobility}"

    party_tags: set[str] = set()
    for comp in intent.get("party", {}).get("composition", []):
        party_tags.update(PARTY_TO_ATOM_TAGS.get(comp, []))
    bad = party_tags & set(constraints.get("unsuitable_for", []))
    if bad:
        return f"unsuitable_for:{sorted(bad)}"

    tier_pref = intent.get("budget", {}).get("tier_preference", 3)
    hard_cap = intent.get("budget", {}).get("hard_cap", False)
    # 非 hard_cap 放宽一档（spec §4 第 1 条）
    allowed_tier = tier_pref if hard_cap else min(4, tier_pref + 1)
    if constraints.get("budget_tier", 1) > allowed_tier:
        return f"budget_tier:{constraints.get('budget_tier')}>allowed:{allowed_tier}"

    for excl in intent.get("explicit_exclusions", []):
        pred = EXCLUSION_PREDICATES.get(excl)
        if pred is not None and pred(atom):
            return f"explicit_exclusion:{excl}"

    return None


def score_atom(intent: dict, atom: dict, weights: Optional[dict] = None) -> float:
    """spec §4 第 2 条加权排序。权重集中在 weights.py，改动需过评测。

    weights=None 时用全局 WEIGHTS（默认基线）；调用方可传入
    weights.effective_weights(intent) 获得 persona 叠加后的有效权重（BACKLOG #11）。
    """
    w = weights or WEIGHTS
    score = 0.0

    season = intent.get("time", {}).get("season")
    if season:
        fit = atom.get("time_structure", {}).get("season_fit", {}).get(season, 0.5)
        score += w["season_fit"] * fit

    goals = set(intent.get("emotion_goals", []))
    tags = set(atom.get("emotion_tags", []))
    score += w["emotion_overlap"] * len(goals & tags)

    if intent.get("pace", {}).get("atoms_per_day", 3) <= RELAXED_ATOMS_PER_DAY:
        crowd = atom.get("constraints", {}).get("crowd_level", 3)
        score += w["crowd_relaxed_pace"] * (6 - crowd)

    if "iconic" in tags:
        score += w["iconic_bonus"]

    # chat soft_constraint（ADR-002 §2.3 v2）：LLM 产出的语义标签权重，
    # 确定性执行——must 命中加分 / avoid 命中降分（软约束，永不剪枝）。
    soft = intent.get("soft_constraints")
    if isinstance(soft, dict):
        must = soft.get("must_tags") or []
        avoid = soft.get("avoid_tags") or []
        if must:
            score += w.get("soft_must_bonus", 0.0) * soft_tag_hits(must, atom)
        if avoid:
            score -= w.get("soft_avoid_penalty", 0.0) * soft_tag_hits(avoid, atom)

    return round(score, 3)


def _risk_flags(intent: dict, atom: dict, today: date) -> list[str]:
    """spec §4 第 4 条预订风险预警 + spec §3.7 事实新鲜度降级。"""
    flags: list[str] = []
    constraints = atom.get("constraints", {})

    if constraints.get("booking_required") == "required":
        lead = constraints.get("booking_lead_days", 0)
        rng = intent.get("time", {}).get("departure_date_range")
        if rng:
            days_to_depart = (date.fromisoformat(rng[0]) - today).days
            if days_to_depart < 0:
                # 出发日期已过：不出现"距出发-5天"这类文案，改为提示重新定档
                flags.append("booking_date_passed:出发日期已过，请重新定档后检查预订")
            elif days_to_depart <= lead:
                flags.append(f"booking_urgent:距出发{days_to_depart}天≤建议提前{lead}天，现在就得订")
            else:
                flags.append("booking_required:需提前预订")
        else:
            flags.append("booking_required_unscheduled:日期未定，定档后须立刻检查预订")

    lv = atom.get("facts", {}).get("last_verified")
    if lv:
        try:
            if (today - date.fromisoformat(lv)).days > STALE_FACT_DAYS:
                flags.append(f"stale_facts:last_verified={lv}，超过{STALE_FACT_DAYS}天未复核")
        except ValueError:
            flags.append(f"stale_facts:last_verified={lv} 不是合法日期")

    return flags


def _assign_slots(items: list[dict], atom_by_id: dict) -> None:
    """按 best_time_of_day 把原子填入日程槽位（确定性贪心：偏好槽占用则填最早空槽）。"""
    taken: set[str] = set()
    for item in items:
        atom = atom_by_id[item["atom_id"]]
        best = atom.get("time_structure", {}).get("best_time_of_day", ["any"])
        pref = BEST_TIME_TO_SLOT.get(best[0], None) if best else None
        if pref is not None and pref not in taken:
            item["slot"] = pref
        else:
            free = [s for s in SLOT_ORDER if s not in taken]
            item["slot"] = free[0] if free else SLOT_ORDER[-1]
        taken.add(item["slot"])
    items.sort(key=lambda i: SLOT_ORDER.index(i["slot"]))


def build_plan(intent: dict, atoms: list[dict], today: Optional[date] = None) -> dict:
    """主入口：IntentVector + 原子库 → 方案草稿 JSON。"""
    today = today or date.today()

    # 1. 硬过滤
    passed: list[dict] = []
    excluded: list[dict] = []
    for atom in atoms:
        reason = hard_filter_reason(intent, atom)
        if reason is None:
            passed.append(atom)
        else:
            excluded.append({"atom_id": atom.get("id"), "reason": reason})

    # 2. 加权排序
    scored = sorted(
        ((score_atom(intent, a), a) for a in passed),
        key=lambda x: x[0],
        reverse=True,
    )

    # 3. 聚类分组：每天绑定 1 个 cluster（spec §4 第 3 条，避免跨城折返）
    clusters: dict[str, list[tuple[float, dict]]] = {}
    for score, atom in scored:
        clusters.setdefault(atom.get("spatial", {}).get("cluster", "_unknown"), []).append((score, atom))
    cluster_rank = sorted(
        clusters.items(),
        key=lambda kv: sum(s for s, _ in kv[1]),
        reverse=True,
    )

    duration = intent.get("time", {}).get("duration_days") or DEFAULT_DURATION_DAYS
    atoms_per_day = intent.get("pace", {}).get("atoms_per_day", 3)
    atom_by_id = {a.get("id"): a for a in atoms}

    days: list[dict] = []
    notes: list[dict] = []
    for day_idx in range(duration):
        if day_idx < len(cluster_rank):
            cluster_name, entries = cluster_rank[day_idx]
            items = []
            for score, atom in entries[:atoms_per_day]:
                matched = sorted(set(intent.get("emotion_goals", [])) & set(atom.get("emotion_tags", [])))
                items.append({
                    "atom_id": atom.get("id"),
                    "name_zh": atom.get("name", {}).get("zh"),
                    "score": score,
                    "matched_emotion_tags": matched,
                    # 一句话理由由叙事层（LLM）在呈现时生成，此处只占位，不放行模型自由发挥
                    "rationale": "【理由占位】待叙事层基于匹配标签生成一句话推荐",
                    "slot": None,
                    "risk_flags": _risk_flags(intent, atom, today),
                })
            _assign_slots(items, atom_by_id)
            days.append({"day": day_idx + 1, "cluster": cluster_name, "items": items})
        else:
            days.append({
                "day": day_idx + 1,
                "cluster": None,
                "items": [],
                "note": "机动日：cluster 数量不足，可二刷喜欢的片区或留作休整",
            })

    for cluster_name, entries in cluster_rank[duration:]:
        notes.append({"note": f"cluster '{cluster_name}' 未排入（天数不足），含 {len(entries)} 个候选原子"})

    # destination_specified 按 schema 可为 list；下游（solver/前端）期望 str：
    # 取第一个并在 notes 说明，不把 list 直通下游
    dest = intent.get("geography", {}).get("destination_specified")
    if isinstance(dest, list):
        notes.append({"note": f"多目的地输入 {dest}，本版按首个「{dest[0]}」出草稿"})
        dest = dest[0] if dest else None

    return {
        "plan_version": "0.1",
        "generated_at": today.isoformat(),
        "destination": dest,
        "duration_days": duration,
        "days": days,
        "excluded_atoms": excluded,
        "notes": notes,
    }
