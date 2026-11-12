"""骨架求解（V1-BUILD §3 首日 3–5 秒预算）。

链路：硬过滤/打分（复用 matcher）→ 聚类分日（每天绑定 1 个 cluster，
天数 > cluster 数时允许机动日）→ cluster 间按与门户/住宿区的距离排序
（最近邻从抵达门户出发，末日靠近离开门户）→ 门户进出挂到首日/末日。

聚类分组与动线排序的实现在 refine.py（refine/skeleton 共用，避免循环依赖）。
"""
from __future__ import annotations

from packages.graph import nodes as gnodes
from .refine import filter_and_score, order_days_by_route, rank_clusters


def solve_skeleton(intent: dict, atoms: list[dict], registry_entry: dict) -> dict:
    """骨架：天数 × 区域 × 门户进出。"""
    dest = registry_entry["id"]
    scored, excluded = filter_and_score(intent, atoms)
    ranked = rank_clusters(scored)
    centroids = gnodes.cluster_centroids(atoms)
    gateway = gnodes.primary_gateway(dest)
    lodging = gnodes.derive_lodging_zones(atoms, registry_entry)

    duration = intent.get("time", {}).get("duration_days") or 3
    chosen = [name for name, _, _ in ranked[:duration]]
    ordered = order_days_by_route(chosen, centroids, gateway)

    days: list[dict] = []
    for i in range(duration):
        if i < len(ordered):
            cname = ordered[i]
            entries = dict((n, e) for n, _, e in ranked)[cname]
            preview = [a.get("name", {}).get("zh") or a.get("name", {}).get("en")
                       for _, a in entries[:3]]
            day = {
                "day": i + 1,
                "cluster": cname,
                "summary": f"第{i + 1}天 · {cname} 片区（{len(entries)} 个候选：{'、'.join(p for p in preview if p)}…）",
                "candidate_count": len(entries),
            }
        else:
            day = {
                "day": i + 1,
                "cluster": None,
                "summary": f"第{i + 1}天 · 机动日（cluster 不足，可二刷或休整）",
                "candidate_count": 0,
            }
        if i == 0 and gateway is not None:
            day["gateway_in"] = {"id": gateway["id"], "name_zh": gateway["name_zh"]}
        if i == duration - 1 and gateway is not None:
            day["gateway_out"] = {"id": gateway["id"], "name_zh": gateway["name_zh"]}
        days.append(day)

    return {
        "destination": dest,
        "duration_days": duration,
        "days": days,
        "lodging_zones": lodging,
        "gateway": gateway,
        "excluded_count": len(excluded),
    }
