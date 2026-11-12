"""L1 叙事生成：入选理由 + 每日主题 + 矛盾显性化措辞。

输入只用 L0 事实（name/summary/categories/budget_tier/energy_cost/best_time_of_day/
落位时刻/风险旗标）+ 意图向量 + persona + 张力检测结果。输出批量 JSON。
"""
from __future__ import annotations

import json
import re

from packages.intent.tensions import conflict_note_template, detect_tensions
from packages.intent.weights import persona_keys
from packages.llm import (MAX_CALLS_PER_PLAN, PROMPT_VERSION, calls_used,
                          chat_text, fingerprint, load_cache, save_cache)

# 空话拦截（顾问体验 §3：禁止空洞赞美）——命中即视为校验失败
_BANNED_PHRASES = ["非常值得一去", "不容错过", "绝绝子", "yyds", "YYDS",
                   "超级出片圣地", "必去打卡圣地", "人间值得"]

_TIME_RE = re.compile(r"(\d{1,2})[:：](\d{2})")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _atom_facts_for_prompt(atom: dict, item: dict) -> dict:
    """prompt 用的 L0 事实集（只放允许引用的字段，从源头掐断幻觉空间）。"""
    c = atom.get("constraints", {})
    ts = atom.get("time_structure", {})
    return {
        "atom_id": atom.get("id"),
        "name_zh": atom.get("name", {}).get("zh") or atom.get("name", {}).get("en"),
        "summary": atom.get("summary"),
        "categories": atom.get("categories", []),
        "budget_tier": c.get("budget_tier"),
        "energy_cost": c.get("energy_cost"),
        "crowd_level": c.get("crowd_level"),
        "best_time_of_day": ts.get("best_time_of_day", []),
        "typical_duration_min": ts.get("typical_duration_min"),
        "slot": item.get("slot"),
        "start": item.get("start"),
        "risk_flags": item.get("risk_flags", []),
    }


def _allowed_numbers(atom: dict, item: dict, intent: dict) -> set[float]:
    """该原子叙事允许出现的数字全集（L0 字段 + 落位事实 + 意图数字）。"""
    out: set[float] = set()
    c = atom.get("constraints", {})
    ts = atom.get("time_structure", {})
    for v in (c.get("budget_tier"), c.get("energy_cost"), c.get("crowd_level"),
              c.get("physical_level"), c.get("price_hint_jpy"), c.get("booking_lead_days"),
              ts.get("typical_duration_min"),
              item.get("duration_min"), item.get("transit_from_prev_min"),
              item.get("distance_from_prev_m"),
              intent.get("time", {}).get("duration_days"),
              intent.get("party", {}).get("size"),
              intent.get("budget", {}).get("per_person_cny")):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.add(float(v))
    for key in ("start", "end"):
        t = item.get(key)
        if isinstance(t, str):
            m = _TIME_RE.match(t)
            if m:
                out.add(float(m.group(1)))
                out.add(float(m.group(2)))
    return out


def fact_check(text: str, atom: dict, item: dict, intent: dict) -> str | None:
    """叙事事实一致性校验。返回失败原因；None = 通过。

    规则：数字/时间必须在 L0 出处集合内；禁止空话短语；长度 ≤ 80 字。
    （限制：中文数字"两小时"不校验——prompt 已要求只用给定事实。）
    """
    if not text or not text.strip():
        return "空叙事"
    for phrase in _BANNED_PHRASES:
        if phrase in text:
            return f"空话拦截：{phrase}"
    if len(text) > 80:
        return f"超长：{len(text)} 字"
    allowed = _allowed_numbers(atom, item, intent)
    for tok in _NUM_RE.findall(text):
        if float(tok) not in allowed:
            return f"数字无出处：{tok}"
    return None


def _build_prompt(entries: list[dict], intent: dict, tensions: list[dict],
                  day_clusters: list[tuple[int, str]]) -> list[dict]:
    persona = persona_keys(intent)
    goals = intent.get("emotion_goals", [])
    lines = [json.dumps(e, ensure_ascii=False) for e in entries]
    tension_txt = "；".join(f"既要{t['x_zh']}又要{t['y_zh']}（处理：{t['handling_zh']}）"
                          for t in tensions) or "无明显张力"
    days_txt = "、".join(f"第{d}天 {c}" for d, c in day_clusters)
    system = (
        "你是资深旅行顾问，为具体的人写具体的入选理由。纪律：\n"
        "1. 每条 ≤60 字，一句说完；必须有主见（可以说\"别赶早\"\"这家比隔壁值\"）；\n"
        "2. 每条至少引用一个给定事实字段（时段/耗时/拥挤/预算档/简介细节），"
        "绝不许出现给定字段之外的任何数字、时间、价格；\n"
        "3. 禁止空洞赞美（\"非常值得一去\"\"不容错过\"这类话直接作废）；\n"
        "4. 像资深顾问对朋友说话，不像广告。\n"
        "输出严格 JSON：{\"narratives\": {\"<atom_id>\": \"...\"}, "
        "\"day_themes\": {\"<天数>\": \"一句当日主题\"}, "
        "\"conflict_note_zh\": \"我注意到你既要X又要Y，我的处理是Z（无张力则 null）\"}"
    )
    user = (
        f"旅行者画像：persona={persona}，情绪目标={goals}，"
        f"意图张力：{tension_txt}\n"
        f"逐日片区：{days_txt}\n"
        f"入选原子（每行一个 JSON，只能引用这些字段）：\n" + "\n".join(lines)
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def narrate_plan(plan: dict, intent: dict, atoms: list[dict], *,
                 plan_id: str = "global") -> dict:
    """plan 入选原子 → L1 叙事。响应形状见 V1-BUILD §4。

    LLM 不可用/预算尽/缓存外失败 → {"llm_used": false, ...模板兜底}，永远 200。
    """
    atom_by_id = {a.get("id"): a for a in atoms}
    # 入选原子（保持 plan 顺序：日 → 槽位）
    entries: list[dict] = []
    items_by_atom: dict[str, dict] = {}
    day_clusters: list[tuple[int, str]] = []
    for d in plan.get("days", []):
        if d.get("cluster"):
            day_clusters.append((d["day"], d["cluster"]))
        for it in d.get("items", []):
            if it.get("type") != "atom" or not it.get("atom_id"):
                continue
            atom = atom_by_id.get(it["atom_id"])
            if atom is None:
                continue
            entries.append(_atom_facts_for_prompt(atom, it))
            items_by_atom[it["atom_id"]] = it

    tensions = detect_tensions(intent)
    template_conflict = conflict_note_template(tensions)
    fp = fingerprint(PROMPT_VERSION, [e["atom_id"] for e in entries],
                     intent.get("party", {}).get("composition"),
                     intent.get("emotion_goals"),
                     intent.get("time", {}).get("season"),
                     intent.get("time", {}).get("departure_date_range"),
                     intent.get("pace", {}).get("atoms_per_day"),
                     intent.get("budget", {}).get("tier_preference"),
                     template_conflict)

    cached = load_cache(fp)
    if cached is not None:
        out = dict(cached)
        out["cache_hit"] = True
        out["llm_used"] = True
        return out

    messages = _build_prompt(entries, intent, tensions, day_clusters)
    raw = chat_text(messages, plan_id=plan_id, timeout=20.0, max_tokens=2500)
    if raw is None:
        # 降级纪律：200 + llm_used=false，矛盾说明走确定性模板（永远可测）
        return {"narratives": {}, "day_themes": {},
                "conflict_note_zh": template_conflict,
                "llm_used": False, "cache_hit": False}

    data = _parse_json(raw)
    if data is None:
        return {"narratives": {}, "day_themes": {},
                "conflict_note_zh": template_conflict,
                "llm_used": False, "cache_hit": False}

    narratives = data.get("narratives") if isinstance(data.get("narratives"), dict) else {}
    # 事实一致性后置校验：无出处 → 自动重写一次 → 仍失败 → 模板收据兜底
    failed: dict[str, str] = {}
    for aid, text in list(narratives.items()):
        atom, item = atom_by_id.get(aid), items_by_atom.get(aid)
        if atom is None or item is None or not isinstance(text, str):
            failed[aid] = "atom 不在方案中或文本非法"
            continue
        why = fact_check(text, atom, item, intent)
        if why:
            failed[aid] = why
    if failed and calls_used(plan_id) < MAX_CALLS_PER_PLAN:
        retry_entries = [e for e in entries if e["atom_id"] in failed]
        retry_msg = _build_prompt(retry_entries, intent, [], day_clusters)
        retry_msg.append({
            "role": "user",
            "content": "上一条叙事未过事实校验（出现了无出处的数字/时间/空话），"
                       "请重写以下原子，只用给定字段：" +
                       ", ".join(f"{e['atom_id']}（{failed[e['atom_id']]}）" for e in retry_entries),
        })
        raw2 = chat_text(retry_msg, plan_id=plan_id, timeout=20.0, max_tokens=1200)
        data2 = _parse_json(raw2) if raw2 else None
        if data2 and isinstance(data2.get("narratives"), dict):
            for aid, text in data2["narratives"].items():
                if aid in failed and isinstance(text, str) and not fact_check(
                        text, atom_by_id[aid], items_by_atom[aid], intent):
                    narratives[aid] = text
                    del failed[aid]
    # 仍失败的项回退模板收据文案
    for aid in failed:
        narratives.pop(aid, None)

    out = {
        "narratives": narratives,
        "day_themes": data.get("day_themes") if isinstance(data.get("day_themes"), dict) else {},
        "conflict_note_zh": data.get("conflict_note_zh") or template_conflict,
        "llm_used": True,
        "cache_hit": False,
        "calls_used": calls_used(plan_id),
        "fact_check_fallback": sorted(failed),  # 回退模板的 atom_id 列表（透明度）
    }
    if not failed:  # 只缓存全量过校验的结果（部分失败的结果下次重试）
        save_cache(fp, {k: out[k] for k in
                        ("narratives", "day_themes", "conflict_note_zh")})
    return out
