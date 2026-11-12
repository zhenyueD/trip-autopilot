"""序列惩罚（BACKLOG #10）：疲劳与叙事是路径依赖的，不是节点独立分。

全部以 delta（增减分）形式作用在候选排序上，由 assign_slots / swap 候选排名消费。
参数唯一来源：packages/intent/weights.py 的 PERSONA_PROFILES / SEQUENCE_DEFAULTS
（经 constraints.compile_constraints 注入），本文件不另立数值。

三部分：
(a) 能量曲线：energy_cost（schema v0.2，缺失时按类别回退，默认 3）
    - 近 3 个已排原子能量和 ≥ 12 → 高耗能（≥4）候选重罚，低耗能（≤2）候选加分
    - 半天能量预算（persona 参数）超出后：低耗能"回血节点"加分，高耗能继续罚
(b) 叙事反差：喧闹（lively_local 情绪 / market_street 类别）→ 安静
    （quiet_escape/healing 情绪 / nature_park·culture_history 类别）转移加分；
    与前一站情绪标签完全同质 → 单调惩罚
(c) 老人/小孩下午回血窗：afternoon_recovery=true 时，下午槽位低耗能候选强加分
    （hard-ish：强惩罚但非 ∞，无低耗能候选时仍可落位）
亲子另加：室内外交替奖励（indoor_outdoor_alternation > 0 时启用）。
"""
from __future__ import annotations

# energy_cost 缺失时的类别回退表（schema v0.2：启发式初标，待数据侧精化覆盖）
ENERGY_FALLBACK_BY_CATEGORY = {
    "food": 1, "onsen_spa": 1, "transport_experience": 1,
    "nature_park": 2, "photo_spot": 2,
    "art_museum": 3, "culture_history": 3, "sightseeing": 3,
    "market_street": 3, "shopping": 3, "viewpoint": 3, "nightlife": 3,
    "activity": 4,
    "day_trip": 5,
}
ENERGY_DEFAULT = 3

# 叙事两极（结构定义，数值权重在 weights.py）
NOISY_TAGS = {"lively_local"}
NOISY_CATEGORIES = {"market_street", "nightlife"}
QUIET_TAGS = {"quiet_escape", "healing"}
QUIET_CATEGORIES = {"nature_park", "culture_history"}

# 序列增减分基值（乘以 persona fatigue_amp 使用；基值本身是结构常量）
FATIGUE_HIGH_PENALTY = 3.0     # 近 3 站高能耗后，再来高耗能的惩罚基值
RECOVERY_BONUS = 2.0           # 回血节点（energy ≤2）奖励基值
CONTRAST_BONUS = 0.8           # 喧闹→安静反差奖励
MONOTONE_PENALTY = 0.6         # 情绪完全同质惩罚
AFTERNOON_RECOVERY_BONUS = 3.0  # 老人/小孩下午回血窗的低耗能强加分（hard-ish）
HIGH_ENERGY_THRESHOLD = 12     # 近 3 站能量和触发疲劳放大的阈值


def energy_cost(atom: dict) -> int:
    """防御式读取 energy_cost：缺失时按类别回退，默认 3（schema v0.2 允许数据暂未覆盖）。"""
    v = atom.get("constraints", {}).get("energy_cost")
    if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 5:
        return v
    cats = atom.get("categories", [])
    if cats:
        return max(ENERGY_FALLBACK_BY_CATEGORY.get(c, ENERGY_DEFAULT) for c in cats)
    return ENERGY_DEFAULT


def _is_noisy(atom: dict) -> bool:
    return bool(set(atom.get("emotion_tags", [])) & NOISY_TAGS) or \
        bool(set(atom.get("categories", [])) & NOISY_CATEGORIES)


def _is_quiet(atom: dict) -> bool:
    return bool(set(atom.get("emotion_tags", [])) & QUIET_TAGS) or \
        bool(set(atom.get("categories", [])) & QUIET_CATEGORIES)


def _same_emotion(a: dict, b: dict) -> bool:
    ta, tb = set(a.get("emotion_tags", [])), set(b.get("emotion_tags", []))
    return bool(ta) and ta == tb


def _is_indoor(atom: dict) -> bool:
    return atom.get("time_structure", {}).get("weather_dependency") in ("indoor_ok", "indoor_only")


def path_delta(candidate: dict, slot: str, placed: list[dict], params: dict) -> float:
    """路径依赖增减分。placed = 本日已排原子（按时间顺序）。确定性。

    params 来自 weights.sequence_params(intent)（persona 覆盖后的有效参数）。
    """
    delta = 0.0
    amp = params.get("fatigue_amp", 1.0)
    budget = params.get("halfday_energy_budget", 9)
    cand_energy = energy_cost(candidate)

    # (a) 能量曲线：近 3 站能量和 + 半天能量预算
    last3 = placed[-3:]
    recent_sum = sum(energy_cost(a) for a in last3)
    half = "am" if slot in ("morning", "noon") else "pm"
    half_placed = [a for a in placed
                   if ("am" if a.get("_slot") in ("morning", "noon") else "pm") == half]
    half_sum = sum(energy_cost(a) for a in half_placed)
    if recent_sum >= HIGH_ENERGY_THRESHOLD and len(last3) >= 3:
        if cand_energy >= 4:
            delta -= FATIGUE_HIGH_PENALTY * amp
        elif cand_energy <= 2:
            delta += RECOVERY_BONUS * amp
    if half_sum + cand_energy > budget:
        if cand_energy <= 2:
            delta += RECOVERY_BONUS * amp   # 回血节点：超预算时低耗能加分
        elif cand_energy >= 4:
            delta -= FATIGUE_HIGH_PENALTY * amp * 0.5

    # (b) 叙事反差 / 单调惩罚（与上一站的关系）
    if placed:
        prev = placed[-1]
        if _is_noisy(prev) and _is_quiet(candidate):
            delta += CONTRAST_BONUS
        if _same_emotion(prev, candidate):
            delta -= MONOTONE_PENALTY
        # 亲子：室内外交替奖励
        alt = params.get("indoor_outdoor_alternation", 0.0)
        if alt and _is_indoor(prev) != _is_indoor(candidate):
            delta += alt

    # (c) 老人/小孩下午回血窗（hard-ish：强加分但非硬禁）
    if params.get("afternoon_recovery") and slot == "afternoon":
        if cand_energy <= 2:
            delta += AFTERNOON_RECOVERY_BONUS
        elif cand_energy >= 4:
            delta -= AFTERNOON_RECOVERY_BONUS * 0.5

    # (d) 低强度偏好（带爸妈/亲子：系统性偏好低耗能原子，与数据分布无关）
    #     BACKLOG #11 补充：能量预算只在 cand_energy≥4 或超支时触发，
    #     当数据侧 energy 分布整体偏低时机制失效；此项提供连续的强度梯度。
    lep = params.get("low_energy_pref", 0.0)
    if lep:
        delta += lep * (3 - cand_energy)

    # persona 槽位偏好（情侣加成傍晚/夜晚，老人降夜场等）
    delta += params.get("prefer_slots", {}).get(slot, 0.0)
    delta += params.get("demote_slots", {}).get(slot, 0.0)

    return round(delta, 3)
