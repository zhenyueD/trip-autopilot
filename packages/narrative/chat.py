"""自然语言改行程（ADR-002 §2.3 / V1.1 PRD P4 对话化部分）。

流程：LLM 把用户话语编译成约束增量 JSON（固定 schema，先校验再执行）
→ 应用到 intent 副本 → solver 全局重解（pinned 不动纪律经 extra_pin_ids 延伸）
→ 返回取舍解释 + diff。LLM 不可用 / 输出非法 → needs_confirm 复述或降级文案，
绝不瞎改方案。
"""
from __future__ import annotations

import copy
import json

from packages.intent.poi_match import _norm, detect_explicit_pois
from packages.llm import PROMPT_VERSION, chat_text
from packages.solver.refine import solve_refine

_ALLOWED_OPS = {"avoid_outdoor", "reduce_intensity", "add_poi",
                "remove_atom", "prefer_category", "slower_day", "soft_constraint"}

_OP_PREFIX_ZH = {
    "avoid_outdoor": "雨天/避晒室内化",
    "reduce_intensity": "降强度",
    "add_poi": "加入点名点位",
    "remove_atom": "移除点位",
    "prefer_category": "偏好加权",
    "slower_day": "放慢节奏",
    "soft_constraint": "偏好加权",
}

# 固定谓词（前 6 个 op）编译不出来时的第 7 条路径：语义层软约束。
# LLM 只产出标签权重，匹配/加减分全在 matcher.soft_tag_hits 的确定性代码里。
_SOFT_TAG_VOCAB_ZH = (
    "情绪标签[healing,lively_local,photo,foodie,romantic,family_fun,nostalgic,luxury,"
    "adventure,quiet_escape,cultural_deep,night_view,bargain,iconic,hidden_gem]、"
    "类别[sightseeing,food,shopping,nature_park,culture_history,nightlife,art_museum,"
    "onsen_spa,market_street,activity,day_trip,transport_experience,photo_spot,viewpoint]、"
    "行为标签[queue(要排队/人挤),crowded(人多),indoor(室内),outdoor(户外)]"
)

_DELTA_SCHEMA_HINT = (
    "输出严格 JSON：{\"op\": \"avoid_outdoor|reduce_intensity|add_poi|remove_atom|"
    "prefer_category|slower_day|soft_constraint|unknown\", \"params\": {...}, "
    "\"understood_zh\": \"一句话复述用户意图\", \"advisor_note_zh\": \"仅 op=unknown 时填\"}\n"
    "op 语义：avoid_outdoor=户外改室内（params 可空）；reduce_intensity=太累了/少排点；"
    "add_poi=想去某地（params:{name}）；remove_atom=去掉某地（params:{name 或 atom_id}）；"
    "prefer_category=多想某类（params:{category}， sightseeing/food/shopping/nature_park/"
    "culture_history/nightlife/art_museum/onsen_spa 之一）；slower_day=某天放缓（params:{day}）；\n"
    "soft_constraint=语义偏好调整，上面 6 个固定操作表达不了的「想要/不想要某种感觉」都走这里"
    "（params:{must_tags:[...], avoid_tags:[...], theme_zh:\"主题词\", "
    "pace_delta:-1|0|1, note_zh:\"一句话取舍说明\"}）。must_tags/avoid_tags 从"
    + _SOFT_TAG_VOCAB_ZH + "中选；"
    "例：「想浪漫点」→ must_tags:[romantic,night_view]；「别安排要排队的地方」→ "
    "avoid_tags:[queue,crowded]；「留半天买手办」→ must_tags:[shopping],pace_delta:-1；"
    "「想看点有历史感的」→ must_tags:[cultural_deep,culture_history]。\n"
    "无法编译成任何可执行调整的请求（如订餐厅/改航班/叫车/问与行程无关的事）→ op=unknown，"
    "并在 advisor_note_zh 里先承认「这个调整超出了我现在能自动执行的范围」，"
    "再以旅行顾问身份给一条具体建议（≤80字，可以结合下面给的当前行程）。只输出 JSON。"
)

# op=unknown 且 LLM 没给 advisor_note_zh（或输出完全不可解析）时的确定性顾问话术
_ADVISOR_FALLBACK_ZH = (
    "这个调整超出了我现在能自动执行的范围。我目前能直接帮你做的：户外改室内、"
    "降强度/放慢节奏、加或去掉某个点、按主题调偏好（比如「想浪漫点」"
    "「别安排要排队的地方」）——换个说法我马上改。"
)


def _plan_digest(plan: dict | None) -> str:
    """当前行程的紧凑摘要，进 chat prompt（编译质量：LLM 看得见方案才改得准）。"""
    if not plan:
        return ""
    parts = []
    for d in plan.get("days", []):
        names = [it.get("name_zh") for it in d.get("items", [])
                 if it.get("type") == "atom" and it.get("name_zh")]
        if names:
            parts.append(f"第{d.get('day')}天：" + "、".join(names[:5]))
    return "当前行程：" + "；".join(parts) if parts else ""


def _sanitize_soft_params(params: object) -> dict | None:
    """soft_constraint 参数的确定性 schema 校验。非法 → None（调用方转 unknown/advisor）。"""
    if not isinstance(params, dict):
        return None

    def tags(v: object) -> list[str] | None:
        if v is None:
            return []
        if not isinstance(v, list):
            return None
        return [t.strip() for t in v if isinstance(t, str) and t.strip()][:8]

    must, avoid = tags(params.get("must_tags")), tags(params.get("avoid_tags"))
    if must is None or avoid is None:
        return None
    pace_delta = params.get("pace_delta", 0)
    if pace_delta not in (-1, 0, 1):
        pace_delta = 0
    if not must and not avoid and pace_delta == 0:
        return None  # 空软约束 = 什么都没编译出来
    return {
        "must_tags": must,
        "avoid_tags": avoid,
        "pace_delta": pace_delta,
        "theme_zh": str(params.get("theme_zh") or "")[:20],
        "note_zh": str(params.get("note_zh") or "")[:120],
    }


def compile_delta(text: str, *, plan_id: str, plan: dict | None = None) -> dict | None:
    """LLM 编译约束增量。返回 None = LLM 不可用；op=unknown = 无法编译（走 advisor_only）。"""
    digest = _plan_digest(plan)
    messages = [
        {"role": "system", "content": "你是旅行顾问的意图编译器。把用户的一句话编译成行程约束增量。"
                                     + _DELTA_SCHEMA_HINT},
        {"role": "user", "content": (digest + "\n用户说：" if digest else "") + text},
    ]
    raw = chat_text(messages, plan_id=plan_id, timeout=15.0, max_tokens=400)
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
        return {"op": "unknown", "params": {}, "understood_zh": "", "advisor_note_zh": ""}
    if not isinstance(data, dict) or data.get("op") not in _ALLOWED_OPS | {"unknown"}:
        return {"op": "unknown", "params": {}, "understood_zh": "", "advisor_note_zh": ""}
    if not isinstance(data.get("params"), dict):
        data["params"] = {}
    if not isinstance(data.get("understood_zh"), str):
        data["understood_zh"] = ""
    if not isinstance(data.get("advisor_note_zh"), str):
        data["advisor_note_zh"] = ""
    if data["op"] == "soft_constraint":
        soft = _sanitize_soft_params(data["params"])
        if soft is None:
            # schema 非法：不硬执行，转顾问路径（保留 LLM 的复述/建议）
            data["op"] = "unknown"
            data["params"] = {}
        else:
            data["params"] = soft
    return data


def _apply_delta(intent: dict, delta: dict, atoms: list[dict]) -> tuple[dict, str | None]:
    """把约束增量应用到 intent 副本。返回 (new_intent, err_zh)。"""
    new = copy.deepcopy(intent)
    op, params = delta["op"], delta["params"]
    if op == "avoid_outdoor":
        if "outdoor_only" not in new["explicit_exclusions"]:
            new["explicit_exclusions"].append("outdoor_only")
    elif op == "reduce_intensity":
        new["pace"]["atoms_per_day"] = max(2, new["pace"].get("atoms_per_day", 3) - 1)
        new["pace"]["rest_afternoon"] = True
    elif op == "slower_day":
        new["pace"]["atoms_per_day"] = max(2, new["pace"].get("atoms_per_day", 3) - 1)
    elif op == "prefer_category":
        cat = str(params.get("category", ""))
        if not cat:
            return new, "没听懂你想多要哪一类"
        if cat not in new["explicit_interests"]:
            new["explicit_interests"].append(cat)
    elif op == "soft_constraint":
        # 语义层软约束：落到 intent.soft_constraints，matcher.score_atom 加权执行
        # （must 命中加分 / avoid 命中降分，均非硬剪枝）。多次 chat 累积合并，
        # 同一标签的新指令覆盖旧偏好（must↔avoid 互斥迁移）。
        sc = new.setdefault("soft_constraints", {"must_tags": [], "avoid_tags": []})
        sc.setdefault("must_tags", [])
        sc.setdefault("avoid_tags", [])
        for t in params["must_tags"]:
            if t in sc["avoid_tags"]:
                sc["avoid_tags"].remove(t)
            if t not in sc["must_tags"]:
                sc["must_tags"].append(t)
        for t in params["avoid_tags"]:
            if t in sc["must_tags"]:
                sc["must_tags"].remove(t)
            if t not in sc["avoid_tags"]:
                sc["avoid_tags"].append(t)
        pd = params.get("pace_delta", 0)
        if pd:
            new["pace"]["atoms_per_day"] = max(1, min(6, new["pace"].get("atoms_per_day", 3) + pd))
        if params.get("theme_zh"):
            sc["theme_zh"] = params["theme_zh"]
        if params.get("note_zh"):
            sc["note_zh"] = params["note_zh"]
    elif op == "add_poi":
        name = str(params.get("name", ""))
        if not name:
            return new, "没听清你想加哪个地方"
        found = detect_explicit_pois(name, atoms, new["geography"].get("destination_specified")
                                     if isinstance(new["geography"].get("destination_specified"), str)
                                     else "")
        if not found:
            return new, f"库内没找到「{name}」——现场原子化（在线核实后自建）在 M1.1c 才开放"
        for aid in found:
            if aid not in new["explicit_pois"]:
                new["explicit_pois"].append(aid)
    elif op == "remove_atom":
        pass  # remove 在 plan 层处理（rejected_ids），不动 intent
    return new, None


def _resolve_remove_target(delta: dict, plan: dict, atoms: list[dict]) -> dict | None:
    """remove_atom 的目标解析：atom_id 精确 → 名称归一化包含。"""
    params = delta["params"]
    by_id = {a.get("id"): a for a in atoms}
    if params.get("atom_id") in by_id:
        return by_id[params["atom_id"]]
    name = _norm(str(params.get("name", "")))
    if not name:
        return None
    plan_ids = {it["atom_id"] for d in plan["days"] for it in d["items"]
                if it.get("type") == "atom" and it.get("atom_id")}
    for aid in sorted(plan_ids):
        a = by_id.get(aid)
        if a is None:
            continue
        names = [a.get("name", {}).get(k) for k in ("zh", "en", "local")]
        if any(name in _norm(n) or _norm(n) in name for n in names if n):
            return a
    return None


def _diff_plans(old: dict, new: dict, atom_by_id: dict) -> dict:
    """新旧 plan 的 atom 槽位 diff（复用 swap 的 diff 合同形状）。"""
    def slots(p):
        return {(d["day"], it.get("slot")): it
                for d in p["days"] for it in d["items"] if it.get("type") == "atom"}

    old_s, new_s = slots(old), slots(new)
    changed: list[dict] = []
    for key in sorted(set(old_s) | set(new_s)):
        o, n = old_s.get(key), new_s.get(key)
        if (o or {}).get("atom_id") == (n or {}).get("atom_id"):
            continue
        day_no, slot = key
        new_items = next((d["items"] for d in new["days"] if d["day"] == day_no), [])
        entry = {
            "day": day_no, "slot": slot,
            "slot_index": new_items.index(n) if n in new_items else None,
            "old": ({"atom_id": o["atom_id"], "name_zh": o.get("name_zh")} if o else None),
            "new": ({"atom_id": n["atom_id"], "name_zh": n.get("name_zh")} if n else None),
        }
        changed.append(entry)
    unchanged = sum(1 for key in set(old_s) & set(new_s)
                    if old_s[key].get("atom_id") == new_s[key].get("atom_id"))
    return {"changed": changed, "unchanged_count": unchanged}


def _tradeoff_zh(op: str, diff: dict, theme_zh: str = "") -> str:
    prefix = _OP_PREFIX_ZH.get(op, "调整")
    if op == "soft_constraint" and theme_zh:
        prefix = f"偏好加权（{theme_zh}）"
    parts = []
    for c in diff["changed"][:3]:
        old_name = (c.get("old") or {}).get("name_zh") or "（空）"
        new_name = (c.get("new") or {}).get("name_zh") or "（留空休整）"
        parts.append(f"第{c['day']}天「{old_name}」→「{new_name}」")
    if not parts:
        return f"{prefix}：约束已生效，方案骨架不变"
    more = f" 等 {len(diff['changed'])} 处" if len(diff["changed"]) > 3 else ""
    return f"{prefix}：" + "；".join(parts) + more


def chat_modify(plan: dict, intent: dict, atoms: list[dict], registry_entry: dict, *,
                text: str, confirmed: bool = False, plan_id: str = "global") -> dict:
    """POST /api/plan/{id}/chat 的实现。永不抛异常给调用方（降级见响应形状）。"""
    delta = compile_delta(text, plan_id=plan_id, plan=plan)
    if delta is None:
        return {"applied": False, "llm_used": False,
                "fallback_zh": "顾问模式暂时不可用，可以用「换一个」手动调整"}
    understood = delta["understood_zh"] or f"你是想：{text}"
    if delta["op"] == "unknown":
        # 顾问式回答（ADR-002 v2）：承认超出自动执行范围 + 给出建议；不瞎改方案。
        # LLM 给了建议用 LLM 的（结合当前行程），没给走确定性兜底话术。
        note = delta.get("advisor_note_zh") or _ADVISOR_FALLBACK_ZH
        return {"applied": False, "llm_used": True, "advisor_only": True,
                "understood_zh": understood, "advisor_note_zh": note, "plan": plan}

    new_intent, err = _apply_delta(intent, delta, atoms)
    if err:
        return {"applied": False, "llm_used": True, "needs_confirm": True,
                "understood_zh": understood, "confirm_zh": err, "plan": plan}

    rejected_add: list[str] = []
    if delta["op"] == "remove_atom":
        target = _resolve_remove_target(delta, plan, atoms)
        if target is None:
            return {"applied": False, "llm_used": True, "needs_confirm": True,
                    "understood_zh": understood,
                    "confirm_zh": "没在当前方案里找到你想去掉的那个点，能说一下具体名字吗？",
                    "plan": plan}
        aid = target["id"]
        name = target.get("name", {}).get("zh") or aid
        pinned = aid in set(intent.get("explicit_pois", [])) or any(
            it.get("atom_id") == aid and it.get("pinned")
            for d in plan["days"] for it in d["items"])
        if pinned and not confirmed:
            # 破坏性操作（移除点名/pinned 项）：先复述确认
            return {"applied": False, "llm_used": True, "needs_confirm": True,
                    "understood_zh": understood,
                    "confirm_zh": f"「{name}」是你之前钉住/点名要去的，确认移除吗？确认后我会重排那一天",
                    "confirm_target": aid, "plan": plan}
        if aid in new_intent.get("explicit_pois", []):
            new_intent["explicit_pois"].remove(aid)
        rejected_add.append(aid)

    # pinned 不动纪律延伸：换入过的原子（非点名 POI）作为 extra pins 参与重解
    poi_ids = set(intent.get("explicit_pois", []))
    extra_pins = [it["atom_id"] for d in plan["days"] for it in d["items"]
                  if it.get("type") == "atom" and it.get("pinned")
                  and it.get("atom_id") and it["atom_id"] not in poi_ids
                  and it["atom_id"] not in rejected_add]

    new_plan = solve_refine(new_intent, atoms, registry_entry, extra_pin_ids=extra_pins)
    new_plan["rejected_ids"] = list(plan.get("rejected_ids", [])) + rejected_add
    atom_by_id = {a.get("id"): a for a in atoms}
    diff = _diff_plans(plan, new_plan, atom_by_id)
    resp = {
        "applied": True, "llm_used": True, "needs_confirm": False,
        "op": delta["op"],  # 透出编译结果（验收/前端可区分固定谓词与 soft_constraint）
        "understood_zh": understood,
        "tradeoff_zh": _tradeoff_zh(delta["op"], diff,
                                    delta["params"].get("theme_zh", "")),
        "diff": diff,
        "plan": new_plan,
        "intent": new_intent,  # 调用方持久化（后续 swap/narrate 用新意图）
    }
    if delta["op"] == "soft_constraint" and delta["params"].get("note_zh"):
        resp["soft_note_zh"] = delta["params"]["note_zh"]
    return resp
