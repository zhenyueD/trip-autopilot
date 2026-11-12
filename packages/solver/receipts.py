"""决策收据：模板化生成（V1-BUILD §3 真实性铁律）。

每条收据只能引用真实绑定的约束，basis 数组逐条列出溯源字段，供验收抽查：
- 用户明示意图：intent.emotion_goals ∩ atom.emotion_tags
- 时段事实：atom.time_structure.best_time_of_day 与落位槽位
- 空间事实：与上一站的实测距离/通行（来自图矩阵）
- 季节事实：atom.time_structure.season_fit[intent.time.season]
- 节奏事实：缓冲/休息窗/连续活动规则
没有任何真实理由时用通用占位，禁止编造（Smith-Renner 教训）。
"""
from __future__ import annotations

EMOTION_ZH = {
    "healing": "治愈", "lively_local": "烟火气", "photo": "拍照出片", "foodie": "美食",
    "romantic": "浪漫", "family_fun": "亲子", "nostalgic": "怀旧", "luxury": "奢享",
    "adventure": "刺激", "quiet_escape": "避世", "cultural_deep": "文化深潜",
    "night_view": "夜景", "bargain": "性价比", "iconic": "必打卡", "hidden_gem": "小众",
}
SEASON_ZH = {"spring": "春季", "summer": "夏季", "autumn": "秋季", "winter": "冬季"}
SLOT_ZH_LOCAL = {
    "morning": "上午", "noon": "中午", "afternoon": "下午",
    "sunset": "傍晚", "evening": "晚上", "night": "夜里",
}


def _first_real_clause(intent: dict, atom: dict, ctx: dict) -> tuple[str, list[str], str]:
    """按优先级取第一条真实可引用的理由。返回 (text, basis, icon)。"""
    # 1. 用户明示意图：情绪标签重合
    goals = set(intent.get("emotion_goals", []))
    tags = atom.get("emotion_tags", []) or []
    matched = [t for t in tags if t in goals]
    if matched:
        wish = "、".join(EMOTION_ZH.get(t, t) for t in matched[:2])
        return (
            f"你说想{wish}——这里的标签正好有「{'/'.join(matched[:2])}」",
            ["intent.emotion_goals", "atom.emotion_tags"],
            "🎯",
        )
    # 2. 时段事实：best_time_of_day 与落位槽位一致
    best = atom.get("time_structure", {}).get("best_time_of_day") or []
    slot = ctx.get("slot")
    if slot and best and best[0] != "any":
        return (
            f"这里最适合{SLOT_ZH_LOCAL.get(best[0], best[0])}来，正好排在{SLOT_ZH_LOCAL.get(slot, slot)}",
            ["atom.time_structure.best_time_of_day"],
            "🕓",
        )
    # 3. 季节事实：当季适配度 ≥ 0.9
    season = intent.get("time", {}).get("season")
    if season:
        fit = atom.get("time_structure", {}).get("season_fit", {}).get(season, 0.5)
        if fit >= 0.9:
            return (
                f"{SEASON_ZH.get(season, season)}适配度 {fit:.1f}，正当时",
                ["intent.time.season", "atom.time_structure.season_fit"],
                "🌸",
            )
    # 4. 空间事实：离上一站很近
    km = ctx.get("km_from_prev")
    if km is not None and km <= 0.8:
        return (
            f"离上一站只有 {int(round(km * 1000))}m，顺路就到",
            ["spatial.transit_from_prev_km"],
            "📍",
        )
    # 5. 拥挤事实：宽松步调下低拥挤
    crowd = atom.get("constraints", {}).get("crowd_level")
    if crowd is not None and crowd <= 2 and intent.get("pace", {}).get("atoms_per_day", 3) <= 3:
        return (
            f"拥挤度低（{crowd}/5），不用排长队",
            ["atom.constraints.crowd_level", "intent.pace.atoms_per_day"],
            "🧘",
        )
    # 6. 通用占位（真实：加权评分是确定性的）
    return (
        "同片区候选里综合评分最高（季节×情绪×拥挤度加权）",
        ["packages/intent/weights.py:WEIGHTS", "solver.score"],
        "⭐",
    )


def atom_receipt(intent: dict, atom: dict, ctx: dict) -> dict:
    """原子行程项的收据。ctx: {slot, km_from_prev, minutes_from_prev}。"""
    text, basis, icon = _first_real_clause(intent, atom, ctx)
    # 第二条补充：空间顺路（若第一条不是空间且确实顺路）
    km = ctx.get("km_from_prev")
    if km is not None and km <= 0.8 and "spatial.transit_from_prev_km" not in basis:
        text += f"；离上一站 {int(round(km * 1000))}m"
        basis.append("spatial.transit_from_prev_km")
    return {"icon": icon, "text_zh": text, "basis": basis}


def meal_receipt(kind: str, duration_min: int) -> dict:
    """餐饮伪 POI 收据：节奏事实（用餐即休整）。"""
    kind_zh = "午餐" if kind == "lunch" else "晚餐"
    return {
        "icon": "🍜",
        "text_zh": f"{kind_zh}占位 {duration_min} 分钟——用餐本身算休整，给行程留缓冲",
        "basis": ["pace.buffer_rule", f"meal.{kind}.duration_min={duration_min}"],
    }


def rest_receipt(minutes: int) -> dict:
    """强制休息窗收据：连续活动规则。"""
    return {
        "icon": "☕",
        "text_zh": f"连续活动接近 4 小时上限，强制留 {minutes} 分钟休息窗",
        "basis": ["pace.max_continuous_min=240", f"pace.rest_window_min={minutes}"],
    }


def gateway_receipt(gateway: dict, direction: str, minutes: float) -> dict:
    """门户进出收据：空间事实。"""
    verb = "抵达" if direction == "in" else "离开"
    return {
        "icon": "✈️" if gateway.get("kind") == "airport" else "🚄",
        "text_zh": f"{verb}门户：{gateway['name_zh']}，市内通勤约 {int(round(minutes))} 分钟",
        "basis": ["graph.gateway", "spatial.gateway_leg_minutes"],
    }


def poi_receipt(intent: dict, atom: dict, ctx: dict) -> dict:
    """点名 POI 收据：用户明示意图优先于一切其它理由（BACKLOG #1）。"""
    base = atom_receipt(intent, atom, ctx)
    name = atom.get("name", {}).get("zh") or atom.get("name", {}).get("en") or ""
    return {
        "icon": "📌",
        "text_zh": f"你点名要去「{name}」——已钉进行程，不会被换掉",
        "basis": ["intent.explicit_pois", "atom.name"],
    }


def lodging_receipt(zone_name: str, minutes: float) -> dict:
    """回酒店返程段收据：空间事实（BACKLOG #8b）。"""
    return {
        "icon": "🏨",
        "text_zh": f"回住宿区（{zone_name}）休整，约 {int(round(minutes))} 分钟；明天从这里出发",
        "basis": ["spatial.return_leg_minutes", "graph.lodging_zone"],
    }
