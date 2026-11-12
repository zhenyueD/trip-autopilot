"""生成演出中间态（V1.1 PRD P2）：从 plan + 原子库导出真实求解中间数据。

演出动画的每一帧必须对应真实中间数据（PRD 验收："不是纯动画"）。
本模块只做只读派生：不改求解逻辑，不写状态，纯确定性。

数据源：solve_refine 产出的 plan（excluded_atoms 带硬过滤原因、days 带逐日分配）
+ 原子库（坐标/cluster）+ graph.nodes.cluster_centroids（质心）。
"""
from __future__ import annotations

from packages.graph.nodes import cluster_centroids

# 硬过滤原因前缀 → 演出字幕用的中文简述（只覆盖编译器实际会产出的 6 种；
# 顺序即匹配顺序，未知前缀兜底原文展示，绝不编造）
_REASON_ZH = [
    ("physical_level", "排除：体力要求超过同行人承受上限"),
    ("destination_mismatch", "排除：不属于本目的地"),
    ("unsuitable_for", "排除：场所标注不适合同行人群"),
    ("budget_tier", "排除：预算档超出本次承受范围"),
    ("explicit_exclusion", "排除：你明确要求避开此类体验"),
    ("booking_window_missed", "排除：预约窗口已错过"),
]

MAX_EXCLUDED_IN_TRACE = 100  # kept=false 抽样上限（体积纪律）


def reason_zh(reason: str) -> str:
    """硬过滤原因 → 一行中文字幕。未知前缀兜底 '排除：<原文>'。"""
    for prefix, zh in _REASON_ZH:
        if reason.startswith(prefix):
            return zh
    return f"排除：{reason}"


def public_atom_view(atom: dict) -> dict:
    """探索模式（PRD P1）的原子公开视图：字段裁剪，内部字段/facts 不外泄。

    media 由图片管道（并行开发）供给；缺失时返回 null，不报错、不编造。
    """
    c = atom.get("constraints", {})
    ts = atom.get("time_structure", {})
    sp = atom.get("spatial", {})
    media = atom.get("media")
    return {
        "id": atom.get("id"),
        "name": {"zh": atom.get("name", {}).get("zh"),
                 "en": atom.get("name", {}).get("en")},
        "categories": atom.get("categories", []),
        "spatial": {"lat": sp.get("lat"), "lon": sp.get("lon"),
                    "cluster": sp.get("cluster")},
        "summary": atom.get("summary"),
        "media": ({
            "image_url": media.get("image_url"),
            "image_source": media.get("image_source"),
            "image_license": media.get("image_license"),
        } if isinstance(media, dict) and media.get("image_url") else None),
        "constraints": {"budget_tier": c.get("budget_tier"),
                        "energy_cost": c.get("energy_cost")},
        "time_structure": {"best_time_of_day": ts.get("best_time_of_day", [])},
        # M1.1b：行家知识（tips）公开白名单——只放文案/适用范围/来源链接/过期时间，
        # confidence/extracted_at 等管道内部字段不外泄；无 tips 返回 []（前端据此隐藏区块）
        "tips": [{
            "tip_zh": t.get("tip_zh"),
            "applies_to": t.get("applies_to", []),
            "source": {"url": (t.get("source") or {}).get("url")},
            "expires_at": t.get("expires_at"),
        } for t in atom.get("tips") or [] if isinstance(t, dict) and t.get("tip_zh")][:3],
    }


def build_generation_trace(plan: dict, atoms: list[dict]) -> dict:
    """plan + 原子库 → generation_trace（PRD P2 演出中间态）。

    - candidates：kept=true 全量（原子 id + 坐标，供全点上图/熄灭动画）；
      kept=false 带 reason_zh 中文字幕，超过 100 条确定性抽样（取排序后前 100）
      并以 excluded_total / excluded_sampled 注明。
    - clusters：入选候选的簇划分 + 质心（供聚簇高亮帧）。
    - day_assignments：逐日 簇 + atom_ids（供连线帧）。
    """
    excluded = plan.get("excluded_atoms", [])
    excluded_ids = {e["atom_id"] for e in excluded}

    kept = [a for a in atoms if a.get("id") not in excluded_ids]
    kept.sort(key=lambda a: a.get("id", ""))  # 确定性

    candidates: list[dict] = [{
        "atom_id": a.get("id"),
        "lat": a.get("spatial", {}).get("lat"),
        "lon": a.get("spatial", {}).get("lon"),
        "kept": True,
    } for a in kept]

    excluded_sorted = sorted(excluded, key=lambda e: e.get("atom_id") or "")
    sampled = len(excluded_sorted) > MAX_EXCLUDED_IN_TRACE
    for e in excluded_sorted[:MAX_EXCLUDED_IN_TRACE]:
        atom = next((a for a in atoms if a.get("id") == e["atom_id"]), None)
        sp = (atom or {}).get("spatial", {})
        candidates.append({
            "atom_id": e["atom_id"],
            "lat": sp.get("lat"),
            "lon": sp.get("lon"),
            "kept": False,
            "reason_zh": reason_zh(e.get("reason", "未知原因")),
        })

    centroids = cluster_centroids(kept)
    kept_by_cluster: dict[str, list[str]] = {}
    for a in kept:
        kept_by_cluster.setdefault(
            a.get("spatial", {}).get("cluster", "_unknown"), []).append(a.get("id"))
    clusters = [{
        "cluster": name,
        "atom_ids": sorted(ids),
        "centroid": {"lat": centroids[name]["lat"], "lon": centroids[name]["lon"]}
                     if name in centroids else None,
    } for name, ids in sorted(kept_by_cluster.items())]

    day_assignments = [{
        "day": d["day"],
        "cluster": d.get("cluster"),
        "atom_ids": [it["atom_id"] for it in d.get("items", [])
                     if it.get("type") == "atom" and it.get("atom_id")],
    } for d in plan.get("days", [])]

    return {
        "total_atoms": len(atoms),
        "candidates": candidates,
        "excluded_total": len(excluded),
        "excluded_sampled": sampled,
        "clusters": clusters,
        "day_assignments": day_assignments,
    }
