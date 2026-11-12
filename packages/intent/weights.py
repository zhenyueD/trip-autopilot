"""排序权重表 —— docs/INTENT-ATOM-SCHEMA.md §4 第 2 条的数值落点。

⚠️ 产品决策集中在此，改动需过评测。
（换模型 / 改 prompt 不许动这里；动这里必须跑评测集并留档，见 docs/ARCHITECTURE.md §6。）

score(atom) =
    WEIGHTS["season_fit"]        * atom.time_structure.season_fit[intent.time.season]   # 季节未指定时该项为 0
  + WEIGHTS["emotion_overlap"]   * |atom.emotion_tags ∩ intent.emotion_goals|
  + WEIGHTS["crowd_relaxed_pace"]* (6 - atom.constraints.crowd_level)                   # 仅 pace 宽松时启用，否则为 0
  + WEIGHTS["iconic_bonus"]                                                             # atom 带 iconic 标签时
"""

WEIGHTS = {
    # 季节适配点乘系数：同一批原子在不同季节排出不同行程，是"千人千面"的核心机制
    "season_fit": 2.0,

    # 情绪目标重合系数：原子的 emotion_tags 与 emotion_goals 重合越多得分越高
    "emotion_overlap": 1.5,

    # 宽松步调下的拥挤惩罚：步调慢的用户更怕排队，crowd_level 越高扣得越多；
    # 特种兵步调（atoms_per_day > RELAXED_ATOMS_PER_DAY）不启用此项
    "crowd_relaxed_pace": 1.0,

    # 必打卡加成：带 iconic 情绪标签的原子获得的固定加分
    "iconic_bonus": 0.5,

    # chat soft_constraint 语义标签加权（ADR-002 §2.3 v2）：只在 intent 带
    # soft_constraints 时生效，无软约束的基线方案分数逐项不变（零回归）
    "soft_must_bonus": 2.0,     # must_tags 每命中 1 个标签的加分
    "soft_avoid_penalty": 3.0,  # avoid_tags 每命中 1 个标签的降分
}

# pace 宽松的判定阈值：atoms_per_day ≤ 3 视为宽松（spec：默认 3 舒适，特种兵 4-5）
RELAXED_ATOMS_PER_DAY = 3


# ---- Persona 动态权重（BACKLOG #11）----
#
# ⚠️ 与上方 WEIGHTS 同规则：产品决策集中在此，改动需过评测。
# 结构：persona 键 = party.composition 枚举；值为「增量 delta」，覆盖到 WEIGHTS 之上
# （base 表永远是默认值；profile 只写差异项）。
# 除 matcher 打分权重外，还携带 solver 序列参数（packages/solver/sequence.py 消费）：
#   fatigue_amp            疲劳动态放大系数（默认 1.0）
#   halfday_energy_budget  半天能量预算（默认 9；超出后高耗能惩罚、低耗能"回血节点"加分）
#   afternoon_recovery     下午 15:00±1h 必须尽量低耗能（老人/小孩；强惩罚但非硬禁）
#   prefer_slots/demote_slots  槽位增减分（如情侣加成傍晚/夜晚，老人降夜场）
#   dinner_anchor_offset   晚餐锚点平移分钟（老人早晚餐 -30）
PERSONA_PROFILES = {
    # 带爸妈：疲劳放大、拥挤厌恶、早晚餐、上午轻量、下午回血窗
    "elderly_parents": {
        "weights": {"crowd_relaxed_pace": +0.5},
        "fatigue_amp": 2.0,
        "halfday_energy_budget": 7,
        "afternoon_recovery": True,
        "prefer_slots": {"morning": +0.5},
        "demote_slots": {"night": -2.0, "evening": -0.5},
        "dinner_anchor_offset": -30,
        "low_energy_pref": 0.8,
    },
    # 情侣：叙事/出片权重上调，傍晚/夜晚槽位加成
    "couple": {
        "weights": {"emotion_overlap": +0.5},
        "fatigue_amp": 1.0,
        "halfday_energy_budget": 9,
        "afternoon_recovery": False,
        "prefer_slots": {"sunset": +1.0, "evening": +1.0, "night": +0.5},
        "demote_slots": {},
        "dinner_anchor_offset": 0,
    },
    # 亲子：室内外交替奖励、排队（拥挤）容忍低、下午回血窗
    "family_kids": {
        "weights": {"crowd_relaxed_pace": +0.5},
        "fatigue_amp": 1.5,
        "halfday_energy_budget": 8,
        "afternoon_recovery": True,
        "prefer_slots": {"morning": +0.3},
        "demote_slots": {"night": -1.5},
        "dinner_anchor_offset": -30,
        "indoor_outdoor_alternation": +0.8,
        "low_energy_pref": 0.4,
    },
    # 朋友结伴：默认略偏夜生活/高强度容忍
    "group_friends": {
        "weights": {},
        "fatigue_amp": 0.8,
        "halfday_energy_budget": 10,
        "afternoon_recovery": False,
        "prefer_slots": {"evening": +0.5, "night": +0.5},
        "demote_slots": {},
        "dinner_anchor_offset": 0,
    },
    # 独行：默认基线
    "solo": {
        "weights": {},
        "fatigue_amp": 1.0,
        "halfday_energy_budget": 9,
        "afternoon_recovery": False,
        "prefer_slots": {},
        "demote_slots": {},
        "dinner_anchor_offset": 0,
    },
    # 特种兵/穷游：放行 75min 禁则（由 atoms_per_day≥4 触发，见 solver/common.py）、
    # 能量预算放宽、疲劳放大下调
    "commando": {
        "weights": {},
        "fatigue_amp": 0.5,
        "halfday_energy_budget": 12,
        "afternoon_recovery": False,
        "prefer_slots": {"morning": +0.5},
        "demote_slots": {},
        "dinner_anchor_offset": 0,
    },
}

# 序列参数默认值（无 persona 命中时使用）
SEQUENCE_DEFAULTS = {
    "fatigue_amp": 1.0,
    "halfday_energy_budget": 9,
    "afternoon_recovery": False,
    "prefer_slots": {},
    "demote_slots": {},
    "dinner_anchor_offset": 0,
    "indoor_outdoor_alternation": 0.0,
    "low_energy_pref": 0.0,
}


def persona_keys(intent: dict) -> list[str]:
    """intent → 命中的 persona 键（有序，确定性）。"""
    comp = intent.get("party", {}).get("composition", [])
    keys = [c for c in comp if c in PERSONA_PROFILES]
    # 特种兵步调覆盖一切人群画像（atoms_per_day ≥ 4 → commando 参数）
    if intent.get("pace", {}).get("atoms_per_day", 3) >= 4:
        keys.append("commando")
    return keys


def effective_weights(intent: dict) -> dict:
    """matcher 打分用的有效权重 = WEIGHTS + 命中 persona 的增量叠加。"""
    eff = dict(WEIGHTS)
    for k in persona_keys(intent):
        for wk, delta in PERSONA_PROFILES[k].get("weights", {}).items():
            eff[wk] = round(eff.get(wk, 0.0) + delta, 4)
    return eff


def sequence_params(intent: dict) -> dict:
    """solver 序列惩罚用的有效参数 = SEQUENCE_DEFAULTS + persona 合并。

    多 persona 命中时（如"带爸妈特种兵" = elderly_parents + commando）：
    - dict 类参数（prefer_slots / demote_slots）按键合并（后者补前者没有的键）；
    - 体力/能量类安全参数取最保守值，不后写覆盖：
      halfday_energy_budget / dinner_anchor_offset 取 min，fatigue_amp /
      low_energy_pref / indoor_outdoor_alternation 取 max，afternoon_recovery 取 any。
      （否则 commando 的 budget=12 会冲掉老人的 7，安全参数被放宽——审查 #13）
    """
    keys = persona_keys(intent)
    params = dict(SEQUENCE_DEFAULTS)
    if not keys:
        return params
    for pk in ("prefer_slots", "demote_slots"):
        merged: dict = {}
        for k in keys:  # persona_keys 有序，确定性
            merged.update(PERSONA_PROFILES[k].get(pk, {}))
        params[pk] = merged
    params["halfday_energy_budget"] = min(
        PERSONA_PROFILES[k].get("halfday_energy_budget", SEQUENCE_DEFAULTS["halfday_energy_budget"])
        for k in keys)
    params["fatigue_amp"] = max(
        PERSONA_PROFILES[k].get("fatigue_amp", SEQUENCE_DEFAULTS["fatigue_amp"]) for k in keys)
    params["afternoon_recovery"] = any(
        PERSONA_PROFILES[k].get("afternoon_recovery", False) for k in keys)
    params["dinner_anchor_offset"] = min(
        PERSONA_PROFILES[k].get("dinner_anchor_offset", 0) for k in keys)
    params["low_energy_pref"] = max(
        PERSONA_PROFILES[k].get("low_energy_pref", 0.0) for k in keys)
    params["indoor_outdoor_alternation"] = max(
        PERSONA_PROFILES[k].get("indoor_outdoor_alternation", 0.0) for k in keys)
    return params
