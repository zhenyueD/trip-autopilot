"""意图张力检测（ADR-002 §2.4）：确定性规则表，永远可测。

检测在这层（确定性），措辞在叙事层（packages/narrative，LLM 可降级到模板）。
每条张力输出：{kind, x_zh, y_zh, handling_zh}——handling_zh 是模板句式，
叙事层可让 LLM 重写措辞，但检测结论永不由 LLM 产生。
"""
from __future__ import annotations

# 同行人构成 → 中文（模板句式用）
_COMP_ZH = {
    "elderly_parents": "爸妈", "family_kids": "孩子", "couple": "另一半",
    "solo": "你一个人", "group_friends": "朋友们", "business": "同事",
}


def detect_tensions(intent: dict) -> list[dict]:
    """IntentVector → 结构化张力列表（确定性，顺序即规则顺序）。"""
    out: list[dict] = []
    party = intent.get("party", {})
    pace = intent.get("pace", {})
    budget = intent.get("budget", {})
    time_ = intent.get("time", {})
    goals = set(intent.get("emotion_goals", []))
    comp = party.get("composition", []) or []
    apd = pace.get("atoms_per_day", 3)
    mobility = party.get("mobility_max_physical_level", 5)
    per_cny = budget.get("per_person_cny")
    tier = budget.get("tier_preference", 3)

    # R1 体力上限 vs 想看密度：带老人/小孩（mobility 收紧）但节奏拉到 4+ 原子/天
    if mobility <= 2 and apd >= 4:
        who = "、".join(_COMP_ZH.get(c, c) for c in comp) or "同行人"
        out.append({
            "kind": "mobility_vs_density",
            "x_zh": f"想一天看 {apd} 个点",
            "y_zh": f"{who}的体力上限只能承受低强度",
            "handling_zh": f"我把每天压到 {min(apd, 3)} 个核心点、下午留了回血窗，宁可少而稳",
        })
    # R2 预算 vs 期待：低预算但点了奢享/高端
    if ("luxury" in goals) and ((per_cny is not None and per_cny <= 3000) or tier <= 2):
        out.append({
            "kind": "budget_vs_luxury",
            "x_zh": "想要奢享感",
            "y_zh": f"人均预算 {int(per_cny)} 元以内" if per_cny else "预算偏紧",
            "handling_zh": "我挑了免费/低价但体验感强的点，把预算留给一两顿好的",
        })
    # R3 避世 vs 打卡：同时要人少清净和必打卡地标
    if ("quiet_escape" in goals or "hidden_gem" in goals) and "iconic" in goals:
        out.append({
            "kind": "quiet_vs_iconic",
            "x_zh": "想去必打卡地标",
            "y_zh": "又想人少清净",
            "handling_zh": "地标排在开门第一波或傍晚团客散后，白天中段走小众点",
        })
    # R4 美食密度 vs 天数短：foodie 且天数 ≤2
    duration = time_.get("duration_days") or 3
    if "foodie" in goals and duration <= 2:
        out.append({
            "kind": "foodie_vs_short",
            "x_zh": "想吃遍当地",
            "y_zh": f"只有 {duration} 天",
            "handling_zh": "我把餐饮密度集中到同一片区，少跑路多吃几家",
        })
    # R5 夜生活 vs 早起困难：想夜景/夜生活但不早起
    if "night_view" in goals and pace.get("early_start_ok") is False:
        out.append({
            "kind": "night_vs_late_start",
            "x_zh": "想看夜景",
            "y_zh": "又不想早起",
            "handling_zh": "白天排得松，把体力留给晚上；夜景点之后直接回住宿区",
        })
    return out


def conflict_note_template(tensions: list[dict]) -> str | None:
    """模板句式（LLM 不可用时的降级措辞，可测）。"""
    if not tensions:
        return None
    t = tensions[0]  # 只说最强的一条（顾问纪律：少而准）
    return f"我注意到你既要{t['x_zh']}，又要{t['y_zh']}——我的处理是：{t['handling_zh']}。"
