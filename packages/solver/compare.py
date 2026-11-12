"""§6 对比实验：有预处理（富化+聚类原子）vs 无预处理（原始 POI 直接规划）。

用法：
    python3 -m packages.solver.compare --dest osaka --intent "清明想和女朋友去大阪，5天，拍照和美食为主"
    python3 -m packages.solver.compare --dest osaka --intent "..." --no-preprocess   # 只跑无预处理
    （默认两种模式都跑并输出指标对比）

- 正常模式：消费 data/atoms/{dest}.json，走完整 solver（skeleton + refine）。
- --no-preprocess：消费 data/interim/candidates_{dest}.json（原始 POI：无情绪标签、
  无聚类、无季节分），solver 退化为「地理最近邻 + 时长截断」——
  每天从住宿锚点出发贪心最近邻，直到当日时间预算耗尽。

指标（两模式同口径）：
- constraint_violations：solver 报告的硬约束违规总数
- cross_cluster_backtracks：同一天内相邻原子跨 cluster 的次数
  （无预处理模式无 cluster 概念，退化为 >5km 的相邻跳跃次数）
- avg_daily_transit_min：平均每日活动间通行分钟（不含门户通勤）
- pacing_violations：半天缓冲 <30min 的天数
两份 plan JSON 输出到 data/experiments/。
"""
from __future__ import annotations

import argparse
import json
import os

from packages.graph import nodes as gnodes
from packages.graph.edges import TravelMatrix
from packages.intent import parser_heuristic
from packages.intent.matcher import load_atoms
from . import common
from .refine import solve_refine

# 原始 POI 的 wv_type → 类别/时长猜测（无富化时的确定性兜底）
WV_TYPE_MAP = {
    "see": ("sightseeing", 90), "do": ("activity", 120), "eat": ("food", 60),
    "buy": ("shopping", 60), "drink": ("nightlife", 90), "sleep": ("lodging", 0),
}


def _load_registry_entry(dest: str) -> dict:
    with open("data/atoms/_registry.json", encoding="utf-8") as f:
        reg = json.load(f)
    for d in reg["destinations"]:
        if d["id"] == dest:
            return d
    raise SystemExit(f"目的地未注册: {dest}")


def _candidates_to_pseudo_atoms(dest: str) -> list[dict]:
    """原始 POI → 伪原子：只有坐标/名称/类别猜测，无情绪标签、无聚类、无季节分。"""
    path = f"data/interim/candidates_{dest}.json"
    with open(path, encoding="utf-8") as f:
        cands = json.load(f)
    if isinstance(cands, dict):
        cands = cands.get("candidates", list(cands.values()))
    out = []
    for i, c in enumerate(cands):
        wv = c.get("wv_type") or "see"
        cat, dur = WV_TYPE_MAP.get(wv, ("sightseeing", 90))
        out.append({
            "id": f"{dest}.raw.{i}",
            "name": {"zh": c.get("name_zh") or c.get("name_en") or c.get("name_local") or f"POI-{i}"},
            "destination": dest,
            "categories": [cat],
            "time_structure": {
                "typical_duration_min": dur,
                "best_time_of_day": ["any"],
                "season_fit": None,  # 无季节分（score_atom 缺省 0.5）
            },
            "spatial": {"lat": c["lat"], "lon": c["lon"], "cluster": None},
            "constraints": {"physical_level": 1, "budget_tier": 2, "crowd_level": 3,
                            "suitable_for": [], "unsuitable_for": []},
            "emotion_tags": [],
        })
    return out


def solve_no_preprocess(intent: dict, pseudo_atoms: list[dict], registry_entry: dict) -> dict:
    """退化求解器：地理最近邻 + 时长截断（无聚类、无情绪、无节奏优化）。"""
    dest = registry_entry["id"]
    matrix = TravelMatrix(dest)
    lodging = gnodes.derive_lodging_zones(
        [{"spatial": {"lat": a["spatial"]["lat"], "lon": a["spatial"]["lon"],
                      "cluster": "all"}} for a in pseudo_atoms],
        registry_entry)
    anchor = (lodging[0]["lat"], lodging[0]["lon"]) if lodging else (
        registry_entry["center_lat"], registry_entry["center_lon"])
    duration = intent.get("time", {}).get("duration_days") or 3
    n_atoms = intent.get("pace", {}).get("atoms_per_day", 3)
    day_start = common.day_start_of(intent)

    remaining = {a["id"]: a for a in pseudo_atoms}
    days: list[dict] = []
    for i in range(duration):
        t = float(day_start)
        prev = anchor
        items: list[dict] = []
        placed = 0
        # 贪心最近邻，直到时间预算（DAY_END - 半天缓冲余量）或数量上限耗尽
        while remaining and placed < n_atoms:
            nxt = min(remaining.values(), key=lambda a: (
                matrix.minutes(prev, (a["spatial"]["lat"], a["spatial"]["lon"])), a["id"]))
            coord = (nxt["spatial"]["lat"], nxt["spatial"]["lon"])
            leg = matrix.leg(prev, coord)
            dur = nxt["time_structure"]["typical_duration_min"]
            if t + leg["minutes"] + dur > common.DAY_END - common.BUFFER_MIN_PER_HALF_DAY:
                break  # 时长截断
            start = t + leg["minutes"]
            items.append({
                "type": "atom", "slot": None, "atom_id": nxt["id"],
                "name_zh": nxt["name"]["zh"],
                "lat": coord[0], "lon": coord[1],
                "start": common.fmt_time(start), "end": common.fmt_time(start + dur),
                "duration_min": dur,
                "transit_from_prev_min": leg["minutes"],
                "distance_from_prev_m": int(round(leg["km"] * 1000)),
                "receipt": {"icon": "📌", "text_zh": "无预处理模式：地理最近邻选择",
                            "basis": ["compare.no_preprocess"]},
                "pinned": False, "risk_flags": [],
            })
            t = start + dur
            prev = coord
            placed += 1
            del remaining[nxt["id"]]
        # 无预处理模式没有半天缓冲概念，如实记录 0 缓冲供指标对比
        days.append({
            "day": i + 1, "cluster": None, "items": items,
            "buffers": {"am_min": 0.0, "pm_min": 0.0},
            "violations": [], "summary": f"第{i + 1}天 · 最近邻退化排线",
        })
    matrix.save()
    return {
        "plan_version": "0.1-no-preprocess",
        "destination": dest, "duration_days": duration,
        "lodging_zones": lodging, "days": days,
        "note": "无预处理：原始 POI + 地理最近邻 + 时长截断（无情绪/聚类/季节/节奏优化）",
    }


# ---------- 指标 ----------

def compute_metrics(plan: dict, preprocessed: bool) -> dict:
    violations = 0
    backtracks = 0
    transit_total = 0.0
    transit_legs = 0
    pacing_violations = 0
    for d in plan["days"]:
        violations += len(d.get("violations", []))
        if d["buffers"]["am_min"] < common.BUFFER_MIN_PER_HALF_DAY:
            pacing_violations += 1
        if d["buffers"]["pm_min"] < common.BUFFER_MIN_PER_HALF_DAY:
            pacing_violations += 1
        atoms = [it for it in d["items"] if it.get("type") == "atom"]
        for j, it in enumerate(atoms):
            if it.get("transit_from_prev_min") and j > 0:
                transit_total += it["transit_from_prev_min"]
                transit_legs += 1
            if j > 0:
                prev = atoms[j - 1]
                if preprocessed:
                    # 跨簇折返：相邻原子不同 cluster（plan item 不带 cluster 时从 id 解析）
                    c1 = (prev.get("atom_id") or "").split(".")
                    c2 = (it.get("atom_id") or "").split(".")
                    if len(c1) >= 3 and len(c2) >= 3 and c1[1] != c2[1]:
                        backtracks += 1
                else:
                    if (it.get("distance_from_prev_m") or 0) > 5000:
                        backtracks += 1  # 无 cluster：>5km 相邻跳跃视为折返
    n_days = max(1, plan["duration_days"])
    return {
        "constraint_violations": violations,
        "cross_cluster_backtracks": backtracks,
        "avg_daily_transit_min": round(transit_total / n_days, 1),
        "pacing_violations": pacing_violations,
        "total_atoms": sum(len([it for it in d["items"] if it.get("type") == "atom"])
                           for d in plan["days"]),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="V1 §6 对比实验：有/无预处理")
    ap.add_argument("--dest", default="osaka")
    ap.add_argument("--intent", required=True)
    ap.add_argument("--no-preprocess", action="store_true", help="只跑无预处理模式")
    ap.add_argument("--out-dir", default="data/experiments")
    args = ap.parse_args()

    entry = _load_registry_entry(args.dest)
    intent = parser_heuristic.parse(args.intent)
    intent["geography"]["destination_specified"] = args.dest
    # BACKLOG #1：点名 POI 锚定（与 API 路径同口径）
    from packages.intent.poi_match import enrich_intent_with_pois
    enrich_intent_with_pois(intent, load_atoms(f"data/atoms/{args.dest}.json"), args.dest)

    os.makedirs(args.out_dir, exist_ok=True)
    results: dict[str, dict] = {}

    if not args.no_preprocess:
        atoms = load_atoms(f"data/atoms/{args.dest}.json")
        plan = solve_refine(intent, atoms, entry)
        results["preprocessed"] = {"plan": plan, "metrics": compute_metrics(plan, True)}
        p = os.path.join(args.out_dir, f"plan_{args.dest}_preprocessed.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=1)
        print(f"[preprocessed] plan → {p}")

    pseudo = _candidates_to_pseudo_atoms(args.dest)
    plan_raw = solve_no_preprocess(intent, pseudo, entry)
    results["no_preprocess"] = {"plan": plan_raw, "metrics": compute_metrics(plan_raw, False)}
    p = os.path.join(args.out_dir, f"plan_{args.dest}_no_preprocess.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(plan_raw, f, ensure_ascii=False, indent=1)
    print(f"[no-preprocess] plan → {p}")

    # 指标对比表
    keys = ["constraint_violations", "cross_cluster_backtracks",
            "avg_daily_transit_min", "pacing_violations", "total_atoms"]
    print(f"\n{'metric':<28}{'preprocessed':>14}{'no_preprocess':>16}")
    for k in keys:
        a = results.get("preprocessed", {}).get("metrics", {}).get(k, "-")
        b = results["no_preprocess"]["metrics"][k]
        print(f"{k:<28}{a!s:>14}{b!s:>16}")


if __name__ == "__main__":
    main()
