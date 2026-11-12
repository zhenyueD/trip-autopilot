"""局部重解（换一个）——V1 的灵魂（V1-BUILD §3 局部重解）。

规则实现：
- 稳定目标项：除被点名槽位外，其余槽位的 atom_id 默认保持不变（Vansteenwegen
  偏离惩罚的极端形式：改动他槽 = 无穷大惩罚）。
- pin 规则：换过的槽位、用户点名的 POI（explicit_pois）永久 pin，
  后续任何重解不得触碰（重排中间档里 pin 槽位连槽位都不动）。
- 重解范围三级（BACKLOG #8a 补全中间档）：
  T1 原位替换：同槽位换原子，其它不动；
  T2 同簇内重排：当日未 pin 原子在槽位间重排（atom 集合只变目标那一个）；
  T3 失败：报 NO_FEASIBLE_ALTERNATIVE。绝不跨日改动（requires_confirmation 恒 false）。
- 被换下的原子进入 plan 级 rejected_ids，永不再次推荐（用户已否决）。
- 约束与打分的唯一事实源：constraints.compile_constraints（BACKLOG #9）。
- BACKLOG #12：swap_candidates() 返回 ≤3 个多样化可行替代（不动 plan）。
"""
from __future__ import annotations

from datetime import date as _date, timedelta as _timedelta

from packages.graph.edges import TravelMatrix, slot_compatible
from packages.intent.matcher import score_atom
from . import common
from . import receipts as rc
from . import sequence
from .constraints import compile_constraints
from .refine import assign_slots, layout_day, pick_meal_suggestions


class SwapError(Exception):
    def __init__(self, code: str, message_zh: str):
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh


def _day_atom_ids(day: dict) -> list[str]:
    return [it["atom_id"] for it in day["items"] if it.get("type") == "atom" and it.get("atom_id")]


def plan_used_atom_ids(plan: dict) -> set[str]:
    used: set[str] = set()
    for d in plan["days"]:
        used.update(_day_atom_ids(d))
    return used


def _proxy_from_item(item: dict) -> dict:
    """把 plan item 还原成 layout_day 可用的 atom 代理（只含布局所需字段）。"""
    return {
        "id": item["atom_id"],
        "name": {"zh": item["name_zh"]},
        "categories": item.get("categories", []),
        "time_structure": {
            "typical_duration_min": item["duration_min"],
            "best_time_of_day": [item["slot"]],  # 落位即其兼容时段
        },
        "spatial": {"lat": item["lat"], "lon": item["lon"], "cluster": None},
        "constraints": {},
        "emotion_tags": [],
    }


class _DayCtx:
    """swap 所需的当日布局上下文（与 refine 口径一致）。"""

    def __init__(self, plan: dict, intent: dict, atoms: list[dict], day: dict, day_no: int):
        self.plan, self.intent, self.day, self.day_no = plan, intent, day, day_no
        self.cluster = day.get("cluster")
        self.day_start = common.day_start_of(intent)
        self.compiled = compile_constraints(intent)
        lodging = plan.get("lodging_zones") or []
        self.primary = next((z for z in lodging if z.get("primary")), lodging[0] if lodging else None)
        self.gateway = plan.get("gateway")
        self.is_first = day_no == 1
        self.is_last = day_no == plan["duration_days"]
        cent = None
        for z in lodging:
            if z.get("cluster") == self.cluster:
                cent = (z["lat"], z["lon"])
        if cent is None:
            for it in day["items"]:
                if it.get("type") == "atom" and it.get("lat") is not None:
                    cent = (it["lat"], it["lon"])
                    break
        self.centroid = cent
        self.start_point = (
            cent if self.is_first else
            (self.primary["lat"], self.primary["lon"]) if self.primary else cent
        )
        self.lodging_point = None if self.is_last else (
            (self.primary["lat"], self.primary["lon"]) if self.primary else None)
        self.dinner_offset = int(self.compiled.sequence_params.get("dinner_anchor_offset", 0))
        # 营业门日期（BACKLOG #2）：出发区间首日 + day_no-1；无日期则回退到
        # plan.generated_at，仍无则 None（合成场景，layout_day 跳过校验门）
        rng = (intent.get("time") or {}).get("departure_date_range")
        base = None
        if rng:
            base = _date.fromisoformat(rng[0])
        elif plan.get("generated_at"):
            base = _date.fromisoformat(plan["generated_at"])
        self.day_date = base + _timedelta(days=day_no - 1) if base else None
        self.cluster_scored = [
            (score_atom(intent, a, self.compiled.soft_weights), a)
            for a in atoms if a.get("spatial", {}).get("cluster") == self.cluster
        ]
        self.cluster_scored.sort(key=lambda x: (-x[0], x[1].get("id", "")))

    def layout(self, slot_map: dict[str, dict], matrix: TravelMatrix) -> dict:
        meal_sug = pick_meal_suggestions(self.cluster, self.cluster_scored,
                                         plan_used_atom_ids(self.plan))
        return layout_day(self.intent, matrix, slot_map,
                          day_start=self.day_start, start_point=self.start_point,
                          gateway_in=self.gateway if self.is_first else None,
                          gateway_out=self.gateway if self.is_last else None,
                          meal_names=meal_sug, center_fallback=self.centroid,
                          lodging_point=self.lodging_point,
                          lodging_name=self.primary["name_zh"] if self.primary else None,
                          dinner_offset_min=self.dinner_offset,
                          day_date=self.day_date)


def _restore_state(intent: dict, old_items: list[dict], new_items: list[dict],
                   new_atom_id: str, new_slot: str, atom_by_id: dict) -> None:
    """拷回 pinned/收据；空间事实变化时按新距离重生成收据（真实性铁律）；
    点名 POI 收据与 pin 状态永远恢复。"""
    poi_ids = set(intent.get("explicit_pois", []))
    old_by_id = {it["atom_id"]: it for it in old_items if it.get("type") == "atom" and it.get("atom_id")}
    for it in new_items:
        if it.get("type") != "atom":
            continue
        aid = it.get("atom_id")
        atom = atom_by_id.get(aid)
        if aid in poi_ids:
            it["pinned"] = True
            if atom is not None:
                it["receipt"] = rc.poi_receipt(intent, atom, {
                    "slot": it["slot"],
                    "km_from_prev": (it.get("distance_from_prev_m") or 0) / 1000.0,
                    "minutes_from_prev": it.get("transit_from_prev_min")})
        elif aid == new_atom_id:
            it["pinned"] = True  # pin 规则：换入的原子永久 pin（T2 重排可能换了槽位）
        elif aid in old_by_id:
            old = old_by_id[aid]
            it["pinned"] = old.get("pinned", False)
            it["name_zh"] = old.get("name_zh")
            it["risk_flags"] = old.get("risk_flags", [])
            if old.get("distance_from_prev_m") == it.get("distance_from_prev_m") \
                    and old.get("slot") == it.get("slot"):
                it["receipt"] = old.get("receipt")
            elif atom is not None:
                it["receipt"] = rc.atom_receipt(intent, atom, {
                    "slot": it["slot"],
                    "km_from_prev": (it.get("distance_from_prev_m") or 0) / 1000.0,
                    "minutes_from_prev": it.get("transit_from_prev_min")})


def _candidate_pool(ctx: _DayCtx, intent: dict, atoms: list[dict],
                    target_atom_id: str, slot: str, meal_only: bool) -> list[tuple[float, dict]]:
    """候选池：同簇、过编译器硬约束、未使用、未被否决、时段兼容。"""
    used = plan_used_atom_ids(ctx.plan) - {target_atom_id}
    rejected = set(ctx.plan.get("rejected_ids", []))
    out: list[tuple[float, dict]] = []
    for a in atoms:
        aid = a.get("id")
        if aid == target_atom_id or aid in used or aid in rejected:
            continue
        if a.get("spatial", {}).get("cluster") != ctx.cluster:
            continue
        if meal_only and "food" not in a.get("categories", []):
            continue
        if ctx.compiled.hard_reason(a) is not None:
            continue
        if not meal_only and not slot_compatible(
                a.get("time_structure", {}).get("best_time_of_day"), slot):
            continue
        out.append((score_atom(intent, a, ctx.compiled.soft_weights), a))
    out.sort(key=lambda x: (-x[0], x[1].get("id", "")))
    return out


def _validate_target(plan: dict, intent: dict, day_no: int, slot_index: int) -> tuple[dict, dict]:
    day = next((d for d in plan["days"] if d["day"] == day_no), None)
    if day is None:
        raise SwapError("DAY_NOT_FOUND", f"第 {day_no} 天不存在")
    items = day["items"]
    if not (0 <= slot_index < len(items)):
        raise SwapError("SLOT_NOT_FOUND", f"第 {day_no} 天没有槽位 {slot_index}")
    target = items[slot_index]
    if target.get("atom_id") in set(intent.get("explicit_pois", [])):
        raise SwapError("SLOT_PINNED", "这是你点名要去的，已被钉住，不能换")
    if target.get("type") not in ("atom", "meal") or not target.get("atom_id"):
        raise SwapError("NOT_SWAPPABLE", "这个槽位（门户/休息/住宿/无建议餐饮）不可替换")
    return day, target


def swap(plan: dict, intent: dict, atoms: list[dict], registry_entry: dict, *,
         day_no: int, slot_index: int, matrix: TravelMatrix,
         target_atom_id: str | None = None) -> dict:
    """执行一次「换一个」：T1 原位 → T2 同簇重排 → T3 报错。原地更新并返回 plan。

    target_atom_id（BACKLOG #12 收尾）：三候选模式里用户选定的那枚候选。
    必须在候选池内（TARGET_NOT_AVAILABLE）；T1 原位不可行则只对它做 T2 重排，
    仍不可行 → TARGET_INFEASIBLE（409）。缺省时保持确定性最高分候选。
    """
    day, target = _validate_target(plan, intent, day_no, slot_index)
    items = day["items"]
    old_atom_id = target["atom_id"]
    slot = target["slot"] if target["type"] == "atom" else (
        "noon" if target.get("meal_kind") == "lunch" else "evening")
    atom_by_id = {a.get("id"): a for a in atoms}
    ctx = _DayCtx(plan, intent, atoms, day, day_no)
    candidates = _candidate_pool(ctx, intent, atoms, old_atom_id, slot,
                                 meal_only=target["type"] == "meal")
    if not candidates:
        raise SwapError("NO_ALTERNATIVE", "同片区没有更多符合条件的候选了")

    picked: dict | None = None
    if target_atom_id is not None:
        hit = next((a for _, a in candidates if a.get("id") == target_atom_id), None)
        if hit is None:
            raise SwapError("TARGET_NOT_AVAILABLE",
                            "所选候选已不可用（可能已被排入、被否决或不满足约束），请重新获取候选列表")
        picked = hit

    if target["type"] == "meal":
        # 餐饮伪 POI：只换建议餐厅，不动时间轴
        cand = picked or candidates[0][1]
        target["atom_id"] = cand.get("id")
        target["lat"] = cand["spatial"]["lat"]
        target["lon"] = cand["spatial"]["lon"]
        kind_zh = "午餐" if target.get("meal_kind") == "lunch" else "晚餐"
        target["name_zh"] = f"{kind_zh}：{cand.get('name', {}).get('zh') or cand.get('name', {}).get('en')}（建议）"
        plan.setdefault("rejected_ids", []).append(old_atom_id)
        total_atoms = sum(len(_day_atom_ids(d)) for d in plan["days"])
        return {"plan": plan, "requires_confirmation": False, "diff": {
            "changed": [{"day": day_no, "slot": slot, "slot_index": slot_index,
                         "old": {"atom_id": old_atom_id, "name_zh": "（原建议餐厅）"},
                         "new": {"atom_id": cand.get("id"), "name_zh": target["name_zh"]},
                         "receipt": target.get("receipt")}],
            "unchanged_count": total_atoms}}

    # ---- T1：原位替换 ----
    def try_inplace(cand: dict) -> dict | None:
        slot_map = {it["slot"]: _proxy_from_item(it)
                    for it in items if it.get("type") == "atom" and it.get("atom_id")}
        slot_map[slot] = cand
        res = ctx.layout(slot_map, matrix)
        return res if res["feasible"] else None

    # ---- T2：同簇内重排（未 pin 原子在当日槽位间重排，atom 集合只变目标）----
    # 注意：重排必须用真实原子（兼容时段以 best_time_of_day 为准），
    # 不能用 _proxy_from_item（其兼容时段被冻结为当前槽位，会锁死重排空间）。
    cur_atoms = {it["slot"]: it["atom_id"]
                 for it in items if it.get("type") == "atom" and it.get("atom_id")}
    pinned_fixed = {s: atom_by_id[aid] for s, aid in cur_atoms.items()
                    if next(it for it in items
                            if it.get("type") == "atom" and it["slot"] == s
                            and it.get("atom_id") == aid).get("pinned")}
    pool = [atom_by_id[aid] for s, aid in cur_atoms.items()
            if s not in pinned_fixed and s != slot and aid in atom_by_id]
    n_atoms = len(cur_atoms)

    def try_resequence(cand: dict, score: float) -> tuple[dict, dict, list[dict]] | None:
        trial_scored = [(score_atom(intent, a, ctx.compiled.soft_weights), a)
                        for a in pool + [cand]]
        trial_scored.sort(key=lambda x: (-x[0], x[1].get("id", "")))
        new_map = assign_slots(trial_scored, n_atoms, fixed=pinned_fixed,
                               seq_params=ctx.compiled.sequence_params)
        # assign_slots 可能根本没把 cand 排进去（兼容槽位被同分原子挤占）→ 不算数
        if cand.get("id") not in {a.get("id") for a in new_map.values()}:
            return None
        res = ctx.layout(new_map, matrix)
        if not res["feasible"]:
            return None
        # 记录重排导致的槽位变化（供 diff；slot_index 对齐 V1-BUILD §4 合同形状）
        extra = []
        for s, a in new_map.items():
            if s in cur_atoms and cur_atoms[s] != a.get("id") \
                    and a.get("id") != cand.get("id"):
                idx = next((i for i, it in enumerate(res["items"])
                            if it.get("type") == "atom" and it.get("atom_id") == a.get("id")), None)
                extra.append({"day": day_no, "slot": s, "slot_index": idx, "kind": "resequence",
                              "old": {"atom_id": cur_atoms[s]},
                              "new": {"atom_id": a.get("id"),
                                      "name_zh": a.get("name", {}).get("zh")}})
        return (cand, res, extra)

    chosen: tuple[dict, dict, list[dict]] | None = None  # (cand, layout, extra_changed)
    if picked is not None:
        # 用户点选候选：T1 原位 → 只对它做 T2 重排；都不行则 409（不降级到别的候选）
        res = try_inplace(picked)
        if res is not None:
            chosen = (picked, res, [])
        else:
            score = next(s for s, a in candidates if a.get("id") == picked.get("id"))
            chosen = try_resequence(picked, score)
            if chosen is None:
                raise SwapError("TARGET_INFEASIBLE",
                                "所选候选放进这一天会破坏节奏/营业约束，请换其它候选")
    else:
        for score, cand in candidates:
            res = try_inplace(cand)
            if res is not None:
                chosen = (cand, res, [])
                break
        if chosen is None:
            for score, cand in candidates[:8]:  # 只对头部候选做重排（确定性 + 限时）
                chosen = try_resequence(cand, score)
                if chosen is not None:
                    break

    if chosen is None:
        raise SwapError("NO_FEASIBLE_ALTERNATIVE",
                        "同片区候选都无法满足当天节奏约束，保持原安排")

    new_atom, res, extra_changed = chosen
    _restore_state(intent, items, res["items"], new_atom.get("id"), slot, atom_by_id)
    day["items"] = res["items"]
    day["buffers"] = res["buffers"]
    day["violations"] = res["violations"]

    plan.setdefault("rejected_ids", [])
    if old_atom_id not in plan["rejected_ids"]:
        plan["rejected_ids"].append(old_atom_id)

    new_item = next(it for it in res["items"]
                    if it.get("type") == "atom" and it.get("atom_id") == new_atom.get("id"))
    total_atoms = sum(len(_day_atom_ids(d)) for d in plan["days"])
    changed = [{
        "day": day_no, "slot": new_item["slot"], "slot_index": res["items"].index(new_item),
        "old": {"atom_id": old_atom_id, "name_zh": target.get("name_zh")},
        "new": {"atom_id": new_atom.get("id"), "name_zh": new_item.get("name_zh")},
        "receipt": new_item.get("receipt"),
    }] + extra_changed
    unchanged = sum(
        1 for d in plan["days"] for it in d["items"]
        if it.get("type") == "atom" and it.get("atom_id")
        and it.get("atom_id") != new_atom.get("id")
        and not any(c.get("kind") == "resequence" and c["new"]["atom_id"] == it.get("atom_id")
                    for c in extra_changed))
    return {"plan": plan, "diff": {"changed": changed, "unchanged_count": unchanged},
            "requires_confirmation": False}


# ---------- BACKLOG #12：三候选模式 ----------

CATEGORY_ZH = {
    "sightseeing": "观光", "food": "餐饮", "market_street": "市集", "shopping": "购物",
    "viewpoint": "观景", "culture_history": "文化历史", "art_museum": "博物馆",
    "nature_park": "自然公园", "onsen_spa": "温泉", "nightlife": "夜生活",
    "activity": "活动", "photo_spot": "出片点", "transport_experience": "交通体验",
    "day_trip": "近郊一日",
}


def _tradeoff_zh(base_atom: dict, cand: dict) -> str:
    """候选与当前原子的差异说明（只引用真实字段）。"""
    parts: list[str] = []
    bc = base_atom.get("constraints", {})
    cc = cand.get("constraints", {})
    if set(cand.get("categories", [])) != set(base_atom.get("categories", [])):
        parts.append("不同类型：" + "/".join(CATEGORY_ZH.get(c, c) for c in cand.get("categories", [])[:2]))
    if cc.get("budget_tier", 0) < bc.get("budget_tier", 0):
        parts.append(f"更便宜（预算档 {cc.get('budget_tier')}）")
    elif cc.get("budget_tier", 0) > bc.get("budget_tier", 0):
        parts.append(f"更贵一些（预算档 {cc.get('budget_tier')}）")
    if cc.get("crowd_level", 3) < bc.get("crowd_level", 3):
        parts.append(f"更清净（拥挤 {cc.get('crowd_level')}/5）")
    e = sequence.energy_cost(cand)
    if e <= 2:
        parts.append("低强度，可当回血站")
    elif e >= 4:
        parts.append("高强度，注意体力")
    return "；".join(parts) if parts else "同片区同类型替代"


def swap_candidates(plan: dict, intent: dict, atoms: list[dict], registry_entry: dict, *,
                    day_no: int, slot_index: int, matrix: TravelMatrix,
                    n: int = 3) -> dict:
    """返回 ≤n 个多样化、T1 可行的替代候选（不动 plan）。BACKLOG #12。

    多样化贪心：第 1 个取综合分最高；第 2 个优先不同主题（categories 不同）；
    第 3 个优先不同预算档。响应形状：
    {candidates: [{atom: {id, name_zh, categories, budget_tier, lat, lon},
                   receipt, tradeoff_zh}], day, slot_index, requires_confirmation: false}
    """
    day, target = _validate_target(plan, intent, day_no, slot_index)
    slot = target["slot"] if target["type"] == "atom" else (
        "noon" if target.get("meal_kind") == "lunch" else "evening")
    ctx = _DayCtx(plan, intent, atoms, day, day_no)
    candidates = _candidate_pool(ctx, intent, atoms, target["atom_id"], slot,
                                 meal_only=target["type"] == "meal")
    base_atom = {a.get("id"): a for a in atoms}.get(target["atom_id"], {})

    feasible: list[tuple[float, dict]] = []
    for score, cand in candidates:
        if target["type"] == "meal":
            feasible.append((score, cand))  # 餐饮替换不动时间轴，恒可行
            continue
        slot_map = {it["slot"]: _proxy_from_item(it)
                    for it in day["items"] if it.get("type") == "atom" and it.get("atom_id")}
        slot_map[slot] = cand
        res = ctx.layout(slot_map, matrix)
        if res["feasible"]:
            feasible.append((score, cand))
        if len(feasible) >= 12:  # 限流：找够多样化素材即停
            break

    picked: list[dict] = []
    for score, cand in feasible:
        if len(picked) >= n:
            break
        if not picked:
            picked.append(cand)
            continue
        # 多样化：与已选候选主题/预算档不同者优先
        if any(set(cand.get("categories", [])) != set(p.get("categories", [])) for p in picked) \
                or any(cand.get("constraints", {}).get("budget_tier")
                       != p.get("constraints", {}).get("budget_tier") for p in picked):
            picked.append(cand)
    # 不足 n 个时按分数补齐（仍是可行的）
    for score, cand in feasible:
        if len(picked) >= n:
            break
        if cand not in picked:
            picked.append(cand)

    out = []
    for cand in picked:
        receipt = rc.atom_receipt(intent, cand, {
            "slot": slot,
            "km_from_prev": None,
            "minutes_from_prev": None,
        })
        out.append({
            "atom": {
                "id": cand.get("id"),
                "name_zh": cand.get("name", {}).get("zh") or cand.get("name", {}).get("en"),
                "categories": cand.get("categories", []),
                "budget_tier": cand.get("constraints", {}).get("budget_tier"),
                "lat": cand.get("spatial", {}).get("lat"),
                "lon": cand.get("spatial", {}).get("lon"),
                "energy_cost": sequence.energy_cost(cand),
            },
            "receipt": receipt,
            "tradeoff_zh": _tradeoff_zh(base_atom, cand),
        })
    return {"candidates": out, "day": day_no, "slot_index": slot_index,
            "requires_confirmation": False}
