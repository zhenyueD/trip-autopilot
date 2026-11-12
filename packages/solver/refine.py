"""全日排线：槽位填充 + 餐饮伪 POI + 硬节奏约束（V1-BUILD §3）。

refine 与 resolve 共用同一个 build_day()，保证"重解口径 = 初始排线口径"，
这是局部重解稳定性的根基（同一确定性算法 → 未触碰槽位不变）。

算法（全部确定性，无随机）：
1. assign_slots：槽位优先贪心 —— 按 SLOT_ORDER 顺序，为每个槽位取
   评分最高、时段兼容、未使用的候选；fixed 槽位直接落位（pin/换原子用）。
2. layout_day：顺序时间轴 —— 门户进出 → 上午 → 中午 → 午餐 → 下午 →
   傍晚 → 晚餐 → 晚上 → 夜里。逐步施加：通行禁则、连续活动休息窗、半天缓冲。
   任一硬约束无法满足 → feasible=False，由调用方换候选/降级。
"""
from __future__ import annotations

from packages.intent.matcher import score_atom
from packages.graph.edges import SLOT_COMPAT, SLOT_ORDER, TravelMatrix, slot_compatible
from packages.verify.gate import check_atom
from . import common
from . import receipts as rc
from . import sequence
from .constraints import CompiledConstraints, compile_constraints


# ---------- 筛选与打分（约束编译器是唯一事实源，BACKLOG #9） ----------

def filter_and_score(intent: dict, atoms: list[dict],
                     compiled: CompiledConstraints | None = None) -> tuple[list[tuple[float, dict]], list[dict]]:
    """硬过滤（编译器剪枝，永不降分）+ 加权排序（persona 有效权重）。返回 (scored desc, excluded)。"""
    compiled = compiled or compile_constraints(intent)
    passed, excluded = [], []
    for a in atoms:
        reason = compiled.hard_reason(a)
        if reason is None:
            passed.append((score_atom(intent, a, compiled.soft_weights), a))
        else:
            excluded.append({"atom_id": a.get("id"), "reason": reason})
    # 确定性：分数降序 → id 字典序
    passed.sort(key=lambda x: (-x[0], x[1].get("id", "")))
    return passed, excluded


# ---------- 槽位分配 ----------

def assign_slots(candidates: list[tuple[float, dict]], n_atoms: int,
                 fixed: dict[str, dict] | None = None,
                 exclude_ids: set[str] | None = None,
                 seq_params: dict | None = None) -> dict[str, dict]:
    """槽位优先贪心 + 路径依赖增减分（BACKLOG #10）。

    fixed: {slot: atom} 直接落位（pin/点名 POI/换原子用）。
    其余槽位按 (基础分 + sequence.path_delta) 排名取最高兼容候选；
    path_delta 依赖本日已排序列（按槽位时间序），实现疲劳/叙事的路径依赖。
    """
    fixed = fixed or {}
    exclude_ids = exclude_ids or set()
    params = seq_params or {}
    slot_map: dict[str, dict] = dict(fixed)
    used = {a.get("id") for a in fixed.values()}
    placed = len(slot_map)
    placed_seq: list[dict] = []  # 已排原子（带 _slot 标注，供 path_delta）
    for slot in SLOT_ORDER:
        if slot in slot_map:
            atom = dict(slot_map[slot])
            atom["_slot"] = slot
            placed_seq.append(atom)
            continue
        if placed >= n_atoms:
            continue
        best: tuple[float, dict] | None = None
        for score, atom in candidates:
            aid = atom.get("id")
            if aid in used or aid in exclude_ids:
                continue
            if not slot_compatible(atom.get("time_structure", {}).get("best_time_of_day"), slot):
                continue
            total = score + sequence.path_delta(atom, slot, placed_seq, params)
            # 确定性：总分降序 → atom_id 字典序升序
            if best is None or total > best[0] or (total == best[0] and aid < best[1].get("id", "")):
                best = (total, atom)
        if best is not None:
            atom = best[1]
            slot_map[slot] = atom
            used.add(atom.get("id"))
            placed += 1
            placed_atom = dict(atom)
            placed_atom["_slot"] = slot
            placed_seq.append(placed_atom)
    return slot_map


# ---------- 时间轴布局 ----------

def _atom_item(atom: dict, slot: str, start: float, leg: dict, intent: dict) -> dict:
    dur = atom.get("time_structure", {}).get("typical_duration_min", 60)
    ctx = {"slot": slot, "km_from_prev": leg.get("km"), "minutes_from_prev": leg.get("minutes")}
    return {
        "type": "atom",
        "slot": slot,
        "atom_id": atom.get("id"),
        "name_zh": atom.get("name", {}).get("zh") or atom.get("name", {}).get("en"),
        "categories": atom.get("categories", []),
        "lat": atom.get("spatial", {}).get("lat"),
        "lon": atom.get("spatial", {}).get("lon"),
        "start": common.fmt_time(start),
        "end": common.fmt_time(start + dur),
        "duration_min": dur,
        "transit_from_prev_min": leg.get("minutes"),
        "distance_from_prev_m": int(round((leg.get("km") or 0) * 1000)),
        "transit_mode": leg.get("mode"),
        "score": ctx.get("score"),
        "receipt": rc.atom_receipt(intent, atom, ctx),
        "pinned": False,
        "risk_flags": [],
    }


def _meal_item(kind: str, name_zh: str, lat, lon, start: float, dur: int,
               suggested_atom_id: str | None) -> dict:
    return {
        "type": "meal",
        "slot": "noon" if kind == "lunch" else "evening",
        "meal_kind": kind,
        "atom_id": suggested_atom_id,  # 伪 POI：挂到真实餐饮原子上（无则 null）
        "pseudo": True,
        "name_zh": name_zh,
        "categories": ["food"],
        "lat": lat, "lon": lon,
        "start": common.fmt_time(start),
        "end": common.fmt_time(start + dur),
        "duration_min": dur,
        "transit_from_prev_min": 0,
        "distance_from_prev_m": 0,
        "receipt": rc.meal_receipt(kind, dur),
        "pinned": False,
        "risk_flags": [],
    }


def _gateway_item(gateway: dict, direction: str, t: float, leg_minutes: float) -> dict:
    name = gateway["name_zh"]
    return {
        "type": "gateway",
        "slot": "morning" if direction == "in" else "night",
        "atom_id": None,
        "name_zh": f"{name}（{'抵达' if direction == 'in' else '离开'}）",
        "categories": [],
        "lat": gateway["lat"], "lon": gateway["lon"],
        "start": common.fmt_time(t),
        "end": common.fmt_time(t + (30 if direction == "in" else 0)),
        "duration_min": 30 if direction == "in" else 0,
        "transit_from_prev_min": leg_minutes,
        "distance_from_prev_m": None,
        "receipt": rc.gateway_receipt(gateway, direction, leg_minutes),
        "pinned": False,
        "risk_flags": [],
    }


def _rest_item(start: float) -> dict:
    return {
        "type": "rest",
        "slot": "afternoon",
        "atom_id": None,
        "name_zh": "休息窗（咖啡/回酒店）",
        "categories": [],
        "lat": None, "lon": None,
        "start": common.fmt_time(start),
        "end": common.fmt_time(start + common.REST_WINDOW_MIN),
        "duration_min": common.REST_WINDOW_MIN,
        "transit_from_prev_min": 0,
        "distance_from_prev_m": 0,
        "receipt": rc.rest_receipt(common.REST_WINDOW_MIN),
        "pinned": False,
        "risk_flags": [],
    }


def layout_day(intent: dict, matrix: TravelMatrix, slot_map: dict[str, dict], *,
               day_start: int, start_point: tuple[float, float],
               gateway_in: dict | None = None, gateway_out: dict | None = None,
               meal_names: dict[str, dict] | None = None,
               center_fallback: tuple[float, float] | None = None,
               lodging_point: tuple[float, float] | None = None,
               lodging_name: str | None = None,
               dinner_offset_min: int = 0,
               day_date: _date | None = None) -> dict:
    """顺序时间轴。返回 {items, feasible, violations, buffers, end_min}。

    硬约束在布局期逐条校验；不可行时 feasible=False 并给出 violations（供换候选）。
    lodging_point：非末日的回酒店返程段锚点（BACKLOG #8b）；末日以 gateway_out 收尾。
    dinner_offset_min：persona 晚餐锚点平移（老人/小孩 -30，BACKLOG #11）。
    day_date：当日日历日期（本地语义）。提供时启用营业时间校验门（BACKLOG #2）：
    落位时刻 closed 的原子视为不可行（换候选），unknown 仅挂 opening_hours_unknown
    风险旗（覆盖率有限，unknown ≠ closed，不惩罚）。
    """
    commando = common.is_commando_pace(intent)
    items: list[dict] = []
    violations: list[str] = []
    failed_atom_ids: list[str] = []
    occupied = {"am": 0.0, "pm": 0.0}

    t = float(day_start)
    streak = 0.0
    prev = start_point

    # 门户抵达段（首日）：结构性通勤，豁免通行禁则
    if gateway_in is not None:
        leg = matrix.leg((gateway_in["lat"], gateway_in["lon"]), prev)
        items.append(_gateway_item(gateway_in, "in", t, leg["minutes"]))
        t += leg["minutes"] + 30  # 通勤 + 入境/取行李余量
        streak += leg["minutes"]
        occupied[common.half_day_of(t)] += leg["minutes"]

    def try_place(atom: dict, slot: str) -> bool:
        """放置一个原子；违反硬约束则记录 violation 并返回 False（不产生副作用）。"""
        nonlocal t, streak, prev
        coord = (atom["spatial"]["lat"], atom["spatial"]["lon"])
        leg = matrix.leg(prev, coord)
        dur = atom.get("time_structure", {}).get("typical_duration_min", 60)

        # 通行禁则：>75min 禁止（特种兵豁免）；>40min 降分在打分层做
        if leg["minutes"] > common.TRANSIT_FORBIDDEN_MIN and not commando:
            violations.append(
                f"transit_forbidden:{prev}->{atom.get('id')}={leg['minutes']}min>75")
            failed_atom_ids.append(atom.get("id"))
            return False

        # 连续活动检查：超过 4h 先插休息窗
        need_rest = streak + leg["minutes"] + dur > common.MAX_CONTINUOUS_MIN
        place_t = t + (common.REST_WINDOW_MIN if need_rest else 0)
        start = max(place_t + leg["minutes"], common.SLOT_ANCHOR.get(slot, day_start))
        if start + dur > common.DAY_END:
            violations.append(f"day_overflow:{atom.get('id')} ends>{common.fmt_time(common.DAY_END)}")
            failed_atom_ids.append(atom.get("id"))
            return False

        # 半天缓冲：占用 = 通行 + 停留（用餐/休息不计）
        hd = common.half_day_of(start)
        span = common.half_day_span(hd, day_start)
        if occupied[hd] + leg["minutes"] + dur > span - common.BUFFER_MIN_PER_HALF_DAY:
            violations.append(
                f"buffer_violation:{hd} occupied={occupied[hd] + leg['minutes'] + dur:.0f}"
                f">span-{common.BUFFER_MIN_PER_HALF_DAY}")
            failed_atom_ids.append(atom.get("id"))
            return False

        # 营业时间校验门（BACKLOG #2）：落位时刻 closed → 不可行换候选；
        # unknown 不惩罚（数据覆盖率有限，unknown ≠ closed），落位后挂风险旗。
        gate: dict | None = None
        if day_date is not None:
            gate = check_atom(atom, _datetime(
                day_date.year, day_date.month, day_date.day,
                int(start) // 60, int(start) % 60))
            if gate["status"] == "closed":
                violations.append(
                    f"closed_at_slot:{atom.get('id')}@{common.fmt_time(start)}: {gate['detail']}")
                failed_atom_ids.append(atom.get("id"))
                return False

        if need_rest:
            items.append(_rest_item(t))
            t += common.REST_WINDOW_MIN
            streak = 0.0
        item = _atom_item(atom, slot, start, leg, intent)
        if gate is not None and gate["status"] == "unknown":
            item["risk_flags"].append("opening_hours_unknown")
        items.append(item)
        occupied[hd] += leg["minutes"] + dur
        streak += leg["minutes"] + dur
        t = start + dur
        prev = coord
        return True

    # 上午 → 中午（午餐前）；若中午原子会把午餐顶过 13:30，则先落午餐
    meal_names = meal_names or {}
    lunch = meal_names.get("lunch")
    lunch_placed = False

    def place_lunch() -> bool:
        """在 max(t, 12:00) 落午餐伪 POI（必须占位）。返回是否成功。"""
        nonlocal t, streak, prev, lunch_placed
        lunch_start = max(t, common.LUNCH_ANCHOR)
        if lunch_start + common.LUNCH_DURATION > common.DAY_END:
            violations.append("day_overflow:lunch")
            return False
        meal_lat = (lunch or {}).get("lat") or (center_fallback or prev)[0]
        meal_lon = (lunch or {}).get("lon") or (center_fallback or prev)[1]
        items.append(_meal_item(
            "lunch", lunch["name_zh"] if lunch else "午餐：附近觅食（参考片区餐馆）",
            meal_lat, meal_lon,
            lunch_start, common.LUNCH_DURATION,
            (lunch or {}).get("atom_id")))
        t = lunch_start + common.LUNCH_DURATION
        streak = 0.0  # 用餐即休整
        prev = (meal_lat, meal_lon)  # 餐位即后续 leg 起点（修正：不再从上午原子算通行）
        lunch_placed = True
        return True

    atom = slot_map.get("morning")
    if atom is not None:
        try_place(atom, "morning")
    noon_atom = slot_map.get("noon")
    if noon_atom is not None:
        coord = (noon_atom["spatial"]["lat"], noon_atom["spatial"]["lon"])
        leg = matrix.leg(prev, coord)
        dur = noon_atom.get("time_structure", {}).get("typical_duration_min", 60)
        if t + leg["minutes"] + dur > 13 * 60 + 30 and t + leg["minutes"] > 11 * 60 + 30:
            place_lunch()
        try_place(noon_atom, "noon")
    if not lunch_placed:
        place_lunch()

    # 下午 → 傍晚
    for slot in ("afternoon", "sunset"):
        atom = slot_map.get(slot)
        if atom is not None:
            try_place(atom, slot)

    # 晚餐伪 POI（必须占位；persona 锚点平移：老人/小孩早晚餐）
    dinner = meal_names.get("dinner")
    dinner_start = max(t, 18 * 60 + dinner_offset_min)
    if dinner_start + common.DURATION_DINNER > common.DAY_END:
        violations.append("day_overflow:dinner")
    else:
        meal_lat = (dinner or {}).get("lat") or (center_fallback or prev)[0]
        meal_lon = (dinner or {}).get("lon") or (center_fallback or prev)[1]
        items.append(_meal_item(
            "dinner", dinner["name_zh"] if dinner else "晚餐：附近觅食（参考片区餐馆）",
            meal_lat, meal_lon,
            dinner_start, common.DURATION_DINNER,
            (dinner or {}).get("atom_id")))
        t = dinner_start + common.DURATION_DINNER
        streak = 0.0
        prev = (meal_lat, meal_lon)  # 餐位即后续 leg 起点（晚/夜场通行从餐厅算）

    # 晚上 → 夜里
    for slot in ("evening", "night"):
        atom = slot_map.get(slot)
        if atom is not None:
            try_place(atom, slot)

    # 门户离开段（末日）
    if gateway_out is not None:
        leg = matrix.leg(prev, (gateway_out["lat"], gateway_out["lon"]))
        items.append(_gateway_item(gateway_out, "out", t, leg["minutes"]))
        occupied[common.half_day_of(t)] += leg["minutes"]
        t += leg["minutes"]
    elif lodging_point is not None:
        # 回酒店返程段（BACKLOG #8b：非末日的晚间锚点是住宿区）
        leg = matrix.leg(prev, lodging_point)
        items.append({
            "type": "lodging",
            "slot": "night",
            "atom_id": None,
            "name_zh": f"回住宿区休整",
            "categories": [],
            "lat": lodging_point[0], "lon": lodging_point[1],
            "start": common.fmt_time(t),
            "end": common.fmt_time(t + leg["minutes"]),
            "duration_min": 0,
            "transit_from_prev_min": leg["minutes"],
            "distance_from_prev_m": int(round(leg["km"] * 1000)),
            "transit_mode": leg["mode"],
            "receipt": rc.lodging_receipt(lodging_name or "住宿区", leg["minutes"]),
            "pinned": False,
            "risk_flags": [],
        })
        occupied[common.half_day_of(t)] += leg["minutes"]
        t += leg["minutes"]

    buffers = {
        "am_min": round(common.half_day_span("am", day_start) - occupied["am"], 1),
        "pm_min": round(common.half_day_span("pm", day_start) - occupied["pm"], 1),
    }
    if buffers["am_min"] < common.BUFFER_MIN_PER_HALF_DAY:
        violations.append(f"buffer_violation:am={buffers['am_min']}")
    if buffers["pm_min"] < common.BUFFER_MIN_PER_HALF_DAY:
        violations.append(f"buffer_violation:pm={buffers['pm_min']}")

    return {
        "items": items,
        "feasible": not violations,
        "violations": violations,
        "failed_atom_ids": failed_atom_ids,
        "buffers": buffers,
        "end_min": t,
    }


# ---------- 餐饮建议：簇内真实 food 原子 ----------

def pick_meal_suggestions(cluster: str, scored: list[tuple[float, dict]],
                          used_ids: set[str]) -> dict[str, dict]:
    """午餐/晚餐建议：簇内未入选的 food 原子（午餐偏 noon，晚餐偏 evening/night）。

    两餐不重复推荐同一家：dinner 排除 lunch 已建议的原子。
    """
    out: dict[str, dict] = {}
    for kind, preferred in (("lunch", {"noon", "any"}), ("dinner", {"evening", "night", "any"})):
        taken = used_ids | {v["atom_id"] for v in out.values() if v.get("atom_id")}
        for score, atom in scored:
            if atom.get("id") in taken:
                continue
            if "food" not in atom.get("categories", []):
                continue
            if atom.get("spatial", {}).get("cluster") != cluster:
                continue
            best = set(atom.get("time_structure", {}).get("best_time_of_day") or [])
            if best & preferred:
                out[kind] = {
                    "atom_id": atom.get("id"),
                    "name_zh": f"{'午餐' if kind == 'lunch' else '晚餐'}：{atom.get('name', {}).get('zh') or atom.get('name', {}).get('en')}（建议）",
                    "lat": atom["spatial"]["lat"], "lon": atom["spatial"]["lon"],
                }
                break
    return out


# ---------- 全日排线驱动（refine 端点的主入口） ----------

from datetime import date as _date, datetime as _datetime, timedelta as _timedelta

from packages.graph import nodes as _gnodes
from packages.graph.edges import haversine_km as _haversine_km
from packages.intent.matcher import _risk_flags as _matcher_risk_flags
from packages.intent.weights import persona_keys as _persona_keys


def rank_clusters(scored: list[tuple[float, dict]]) -> list[tuple[str, float, list[tuple[float, dict]]]]:
    """聚类分组 + 按总评分排序（matcher §4 第 3 条约定：每天绑定 1 个 cluster）。"""
    by_cluster: dict[str, list[tuple[float, dict]]] = {}
    for score, atom in scored:
        by_cluster.setdefault(atom.get("spatial", {}).get("cluster", "_unknown"), []).append((score, atom))
    ranked = [
        (name, sum(s for s, _ in entries), entries)
        for name, entries in by_cluster.items()
    ]
    # 确定性：总分降序 → 名称字典序
    ranked.sort(key=lambda x: (-x[1], x[0]))
    return ranked


def order_days_by_route(cluster_names: list[str], centroids: dict[str, dict],
                        gateway: dict | None) -> list[str]:
    """cluster 间动线排序：从门户出发的最近邻链（确定性贪心）。"""
    remaining = list(cluster_names)
    if gateway is not None:
        cur = (gateway["lat"], gateway["lon"])
    else:
        cur = (centroids[remaining[0]]["lat"], centroids[remaining[0]]["lon"]) if remaining else (0.0, 0.0)
    ordered: list[str] = []
    while remaining:
        # 确定性：距离升序 → 名称字典序
        nxt = min(remaining, key=lambda c: (
            _haversine_km(cur[0], cur[1], centroids[c]["lat"], centroids[c]["lon"]), c))
        ordered.append(nxt)
        remaining.remove(nxt)
        cur = (centroids[nxt]["lat"], centroids[nxt]["lon"])
    return ordered


def _fixed_assignments(pinned_atoms: list[dict]) -> list[dict[str, dict]]:
    """点名 POI 的 fixed 槽位分配变体（确定性枚举）。

    每个 POI 的可行槽 = best_time_of_day 兼容槽位（SLOT_ORDER 序）；
    返回无槽位冲突的全部组合，按"尽量早"排序 —— 早槽不可行时（如抵达日
    AM 窗口放不下长时长原子）自动退化到下午，保证点名 POI 必被排入。
    """
    if not pinned_atoms:
        return [{}]
    compat: list[list[str]] = []
    for a in pinned_atoms:
        slots = [s for s in SLOT_ORDER
                 if slot_compatible(a.get("time_structure", {}).get("best_time_of_day"), s)]
        compat.append(slots or ["morning"])
    variants: list[dict[str, dict]] = []

    def rec(i: int, acc: dict[str, dict]):
        if i == len(pinned_atoms):
            variants.append(dict(acc))
            return
        for s in compat[i]:
            if s not in acc:
                acc[s] = pinned_atoms[i]
                rec(i + 1, acc)
                del acc[s]

    rec(0, {})
    # 确定性排序：槽位索引元组升序（尽量早的变体优先）
    variants.sort(key=lambda v: tuple(SLOT_ORDER.index(s) for s in v))
    return variants


def _solve_one_day(intent: dict, matrix: TravelMatrix, *,
                   cluster: str, cluster_scored: list[tuple[float, dict]],
                   n_atoms: int, day_start: int, start_point: tuple[float, float],
                   gateway_in: dict | None, gateway_out: dict | None,
                   globally_used: set[str], centroid: tuple[float, float],
                   pinned_atoms: list[dict] | None = None,
                   seq_params: dict | None = None,
                   lodging_point: tuple[float, float] | None = None,
                   lodging_name: str | None = None,
                   dinner_offset_min: int = 0,
                   day_date: _date | None = None) -> dict:
    """单日益排：assign_slots + layout_day + 确定性修复循环（丢弃不可行原子重选）。

    pinned_atoms：点名 POI。修复循环永不丢弃 pin 原子；所有 fixed 槽位变体
    都不可行时才如实带 violations 返回（点名 POI 锚定是产品承诺，优先于缓冲）。
    """
    pinned_atoms = pinned_atoms or []
    fixed_ids = {a.get("id") for a in pinned_atoms}
    best_res: dict | None = None
    best_map: dict[str, dict] = {}
    for fixed in _fixed_assignments(pinned_atoms):
        excluded: set[str] = set()
        for _ in range(n_atoms + 3):
            slot_map = assign_slots(
                cluster_scored, n_atoms,
                fixed=fixed,
                exclude_ids=(globally_used | excluded) - fixed_ids,
                seq_params=seq_params)
            if not slot_map:
                res = {"items": [], "feasible": True, "violations": [], "failed_atom_ids": [],
                       "buffers": {"am_min": common.half_day_span("am", day_start),
                                   "pm_min": common.half_day_span("pm", day_start)},
                       "end_min": float(day_start)}
                break
            meal_sug = pick_meal_suggestions(
                cluster, cluster_scored, globally_used | excluded | {a.get("id") for a in slot_map.values()})
            res = layout_day(
                intent, matrix, slot_map,
                day_start=day_start, start_point=start_point,
                gateway_in=gateway_in, gateway_out=gateway_out,
                meal_names=meal_sug, center_fallback=centroid,
                lodging_point=lodging_point, lodging_name=lodging_name,
                dinner_offset_min=dinner_offset_min,
                day_date=day_date)
            if res["feasible"]:
                return {"slot_map": slot_map, "layout": res}
            # 记录最优变体（violations 最少），供全部失败时兜底
            if best_res is None or len(res["violations"]) < len(best_res["violations"]):
                best_res, best_map = res, slot_map
            repairable = [aid for aid in res["failed_atom_ids"] if aid not in fixed_ids]
            if repairable:
                excluded.update(repairable)
            else:
                # 结构性溢出：从最低优先级非 fixed 槽位开始减原子
                for slot in reversed(SLOT_ORDER):
                    if slot in slot_map and slot_map[slot].get("id") not in fixed_ids:
                        excluded.add(slot_map[slot].get("id"))
                        break
                else:
                    break  # 只剩 fixed 原子：换下一个 fixed 变体
    if best_res is None:
        # 崩溃路径防御（理论不可达：variants 至少 [{}]，slot_map 至少含 fixed；
        # 但一旦被未来改动触发，必须优雅降级而非调用方 TypeError）：
        # 当日留空 + 违规说明，由 day["violations"] 透传给前端如实展示
        best_res = {"items": [], "feasible": False,
                    "violations": ["no_feasible_layout:当日候选全部布局失败，已留空"],
                    "failed_atom_ids": [],
                    "buffers": {"am_min": common.half_day_span("am", day_start),
                                "pm_min": common.half_day_span("pm", day_start)},
                    "end_min": float(day_start)}
    return {"slot_map": best_map, "layout": best_res}


def solve_refine(intent: dict, atoms: list[dict], registry_entry: dict,
                 today: _date | None = None,
                 extra_pin_ids: list[str] | None = None) -> dict:
    """完整方案：逐日 assign_slots + layout_day，带真实阶段耗时（供 API stages 字段）。

    BACKLOG #1：intent.explicit_pois 点名的原子 pin 进其 cluster 所在日（硬过滤仍适用），
    其 cluster 强制进入分日清单；收据标注"你点名要去"。
    ADR-002 §2.3：extra_pin_ids（chat 重解时"换一个"换入的原子）同样 pin 进所在日，
    但收据不升级为点名文案（pin 纪律延伸，语义不混淆）。
    """
    import time as _time

    today = today or _date.today()
    dest = registry_entry["id"]
    stages: list[dict] = []

    t0 = _time.perf_counter()
    compiled = compile_constraints(intent, today)
    scored, excluded = filter_and_score(intent, atoms, compiled)
    seq_params = compiled.sequence_params
    stages.append({"stage": "filter_score", "name_zh": "约束编译·硬过滤与加权打分",
                   "ms": round((_time.perf_counter() - t0) * 1000, 1)})

    t1 = _time.perf_counter()
    ranked = rank_clusters(scored)
    centroids = _gnodes.cluster_centroids(atoms)
    gateway = _gnodes.primary_gateway(dest)
    lodging = _gnodes.derive_lodging_zones(atoms, registry_entry)
    matrix = TravelMatrix(dest)
    duration = intent.get("time", {}).get("duration_days") or 3
    atom_by_id = {a.get("id"): a for a in atoms}

    # 点名 POI：过硬约束的原子参与锚定；其 cluster 强制进入分日清单
    poi_atoms = [atom_by_id[i] for i in intent.get("explicit_pois", []) if i in atom_by_id]
    poi_ok = [a for a in poi_atoms if compiled.hard_reason(a) is None]
    poi_dropped = [a for a in poi_atoms if compiled.hard_reason(a) is not None]
    poi_ids = {a.get("id") for a in poi_ok}
    # chat 重解的 extra pins（换入原子）：同样钉进所在日，收据不升级
    explicit_set = set(intent.get("explicit_pois", []))
    extra_ok = [atom_by_id[i] for i in (extra_pin_ids or [])
                if i in atom_by_id and i not in explicit_set
                and compiled.hard_reason(atom_by_id[i]) is None]
    extra_ids = {a.get("id") for a in extra_ok}
    poi_by_cluster: dict[str, list[dict]] = {}
    for a in poi_ok + extra_ok:
        poi_by_cluster.setdefault(a.get("spatial", {}).get("cluster", "_unknown"), []).append(a)

    chosen = [name for name, _, _ in ranked[:duration]]
    for cname in sorted(poi_by_cluster):  # 确定性：字典序强制补入
        if cname not in chosen:
            chosen.append(cname)
    # 天数有限时点名片优先，挤掉排名最末的非点名片
    chosen = chosen[:duration] if len(chosen) <= duration else (
        sorted(poi_by_cluster) + [c for c in chosen if c not in poi_by_cluster])[:duration]
    ordered = order_days_by_route(chosen, centroids, gateway)
    stages.append({"stage": "cluster_days", "name_zh": "聚类分日与动线排序",
                   "ms": round((_time.perf_counter() - t1) * 1000, 1)})

    t2 = _time.perf_counter()
    day_start = common.day_start_of(intent)
    n_atoms = intent.get("pace", {}).get("atoms_per_day", 3)
    primary_lodging = lodging[0] if lodging else None
    globally_used: set[str] = set(poi_ids) | extra_ids  # 点名+pinned 原子全局占用，防止别的日子重复选
    entries_by_cluster = {n: e for n, _, e in ranked}
    # 营业门日期基准（BACKLOG #2）：有出发区间用区间首日，否则用 today；
    # 第 i 天 = base + i。layout_day 内部据此校验落位时刻营业状态。
    rng = (intent.get("time") or {}).get("departure_date_range")
    base_date = _date.fromisoformat(rng[0]) if rng else today

    days: list[dict] = []
    for i in range(duration):
        is_first, is_last = i == 0, i == duration - 1
        if i < len(ordered):
            cname = ordered[i]
            cent = centroids[cname]
            # 动线起点：非首日从 primary 住宿区出发；首日的门户通勤段由
            # gateway_in 单独建边（门户→本日 cluster 质心，结构性通勤豁免禁则）
            start_point = (
                (cent["lat"], cent["lon"]) if is_first
                else (primary_lodging["lat"], primary_lodging["lon"]) if primary_lodging
                else (cent["lat"], cent["lon"])
            )
            # 点名 POI → pinned 原子列表（槽位变体由 _fixed_assignments 枚举，
            # 早槽不可行自动退化到下午，保证必被排入）
            pinned_atoms = poi_by_cluster.get(cname, [])
            # 回酒店返程锚点（非末日）；末日以 gateway_out 收尾
            lodging_point = None if is_last else (
                (primary_lodging["lat"], primary_lodging["lon"]) if primary_lodging else None)
            solved = _solve_one_day(
                intent, matrix,
                cluster=cname, cluster_scored=entries_by_cluster.get(cname, []),
                n_atoms=n_atoms, day_start=day_start, start_point=start_point,
                gateway_in=gateway if is_first else None,
                gateway_out=gateway if is_last else None,
                globally_used=globally_used,
                centroid=(cent["lat"], cent["lon"]),
                pinned_atoms=pinned_atoms, seq_params=seq_params,
                lodging_point=lodging_point,
                lodging_name=primary_lodging["name_zh"] if primary_lodging else None,
                dinner_offset_min=int(seq_params.get("dinner_anchor_offset", 0)),
                day_date=base_date + _timedelta(days=i))
            for atom in solved["slot_map"].values():
                globally_used.add(atom.get("id"))
            items = solved["layout"]["items"]
            for it in items:
                if it.get("atom_id") and it["type"] == "atom":
                    atom = atom_by_id[it["atom_id"]]
                    # 合并 matcher 风险旗与 layout 期营业门风险旗（opening_hours_unknown）
                    flags = _matcher_risk_flags(intent, atom, today)
                    for f in it["risk_flags"]:
                        if f not in flags:
                            flags.append(f)
                    it["risk_flags"] = flags
                    if it["atom_id"] in poi_ids:
                        # 点名 POI：永久 pin + 专属收据（"你点名要去"）
                        it["pinned"] = True
                        it["receipt"] = rc.poi_receipt(intent, atom, {
                            "slot": it["slot"],
                            "km_from_prev": (it.get("distance_from_prev_m") or 0) / 1000.0,
                            "minutes_from_prev": it.get("transit_from_prev_min"),
                        })
                    elif it["atom_id"] in extra_ids:
                        # chat 重解的 extra pin（换入原子）：pin 但收据不升级
                        it["pinned"] = True
            days.append({
                "day": i + 1,
                "cluster": cname,
                "items": items,
                "buffers": solved["layout"]["buffers"],
                "violations": solved["layout"]["violations"],
                "gateway_in": {"name_zh": gateway["name_zh"]} if is_first and gateway else None,
                "gateway_out": {"name_zh": gateway["name_zh"]} if is_last and gateway else None,
                "summary": f"第{i + 1}天 · {cname} 片区",
            })
        else:
            days.append({
                "day": i + 1,
                "cluster": None,
                "items": [],
                "buffers": {"am_min": common.half_day_span("am", day_start),
                            "pm_min": common.half_day_span("pm", day_start)},
                "violations": [],
                "gateway_in": None,
                "gateway_out": {"name_zh": gateway["name_zh"]} if is_last and gateway else None,
                "summary": f"第{i + 1}天 · 机动日（可二刷喜欢的片区或休整）",
            })
    stages.append({"stage": "slot_fill", "name_zh": "槽位填充与节奏硬约束校验",
                   "ms": round((_time.perf_counter() - t2) * 1000, 1)})

    t3 = _time.perf_counter()
    matrix.save()
    stages.append({"stage": "persist_matrix", "name_zh": "通行矩阵落盘缓存",
                   "ms": round((_time.perf_counter() - t3) * 1000, 1)})

    plan = {
        "plan_version": "0.2",
        "destination": dest,
        "duration_days": duration,
        "generated_at": today.isoformat(),
        "lodging_zones": lodging,
        "gateway": gateway,
        "days": days,
        "excluded_atoms": excluded,
        "stages": stages,
        "persona": _persona_keys(intent),
    }
    if poi_dropped:
        plan["poi_notes"] = [
            f"你点名的「{a.get('name', {}).get('zh') or a.get('name', {}).get('en')}」"
            f"因硬约束未排入：{compiled.hard_reason(a)}" for a in poi_dropped]
    return plan
