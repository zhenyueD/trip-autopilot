"""OSRM 通行矩阵预计算（BACKLOG #5）。

用法：
    python3 -m packages.graph.build_matrices [--dest osaka] [--offline] [--compare]

预计算范围（每个目的地）：
1. cluster 质心 ↔ cluster 质心 全矩阵
2. gateway ↔ cluster 质心
3. 簇内 atom ↔ atom（仅 haversine > 1.2km 的腿；≤1.2km 短腿按规范留给
   edges.py 的步行 haversine 估算，不消耗 OSRM 配额）

profile 规则：>1.2km 且 ≤3km → foot；>3km → driving（市内轨道代理，
见 routing.py 文档；Transitous 真实公交为未来工作）。

网络失败时自动回退 haversine 估算并在缓存条目标记 source=haversine_estimate。
--compare 打印 haversine vs OSRM 的跨簇通行时间对比（构建后读缓存，零额外请求）。
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys

from packages.graph import nodes, routing
from packages.graph.edges import TravelMatrix, haversine_km, WALK_MAX_KM

TABLE_CHUNK = 90          # demo 服务器 table 坐标上限约 100，留余量
ATOMS_DIR = "data/atoms"


def _load_registry() -> dict:
    with open(os.path.join(ATOMS_DIR, "_registry.json"), encoding="utf-8") as f:
        return json.load(f)


def _load_atoms(dest: str) -> list[dict]:
    with open(os.path.join(ATOMS_DIR, f"{dest}.json"), encoding="utf-8") as f:
        return json.load(f)["atoms"]


def _fill_from_table(cache: routing.RoutingCache, client: routing.OSRMClient,
                     points: list[tuple[float, float]], pairs: list[tuple[int, int]],
                     profile: str, stats: dict) -> None:
    """一次（或分块）table 请求填充一组 pair。失败/缺单元格 → haversine 兜底。"""
    # 分块：每块独立 table，pair 按块内索引重映射；跨块 pair 逐个 route 查询
    chunks = [points[i:i + TABLE_CHUNK] for i in range(0, len(points), TABLE_CHUNK)]
    index_of = {p: i for i, p in enumerate(points)}

    def chunk_id(idx: int) -> int:
        return idx // TABLE_CHUNK

    intra_chunk: dict[int, list[tuple[int, int]]] = {}
    cross_chunk: list[tuple[int, int]] = []
    for i, j in pairs:
        if chunk_id(i) == chunk_id(j):
            intra_chunk.setdefault(chunk_id(i), []).append((i, j))
        else:
            cross_chunk.append((i, j))

    for cid, cpairs in intra_chunk.items():
        base = cid * TABLE_CHUNK
        block = chunks[cid]
        table = client.table(block, profile)
        stats["requests"] = client.requests_made
        for i, j in cpairs:
            li, lj = i - base, j - base
            entry = None
            if table:
                d_fwd = table["durations_min"][li][lj]
                d_bwd = table["durations_min"][lj][li]
                durs = [d for d in (d_fwd, d_bwd) if d is not None]
                if durs:
                    km = haversine_km(*points[i], *points[j])
                    if table["distances_km"]:
                        ks = [k for k in (table["distances_km"][li][lj],
                                          table["distances_km"][lj][li]) if k is not None]
                        route_km = sum(ks) / len(ks) if ks else km
                    else:
                        route_km = km
                    # 对称缓存：往返取均值
                    entry = (sum(durs) / len(durs), route_km, routing.SOURCE_OSM)
            if entry is None:
                m, km, _ = TravelMatrix.estimate_minutes(points[i], points[j])
                entry = (m, km, routing.SOURCE_HAVERSINE)
                stats["fallback"] += 1
            else:
                stats["osrm_ok"] += 1
            cache.put(profile, points[i], points[j], *entry)
            stats["filled"] += 1

    for i, j in cross_chunk:
        r = client.route(points[i], points[j], profile)
        stats["requests"] = client.requests_made
        if r:
            cache.put(profile, points[i], points[j], r["minutes"], r["km"],
                      routing.SOURCE_OSM)
            stats["osrm_ok"] += 1
        else:
            m, km, _ = TravelMatrix.estimate_minutes(points[i], points[j])
            cache.put(profile, points[i], points[j], m, km, routing.SOURCE_HAVERSINE)
            stats["fallback"] += 1
        stats["filled"] += 1


def build_dest(dest: str, offline: bool = False, quiet: bool = False) -> dict:
    atoms = _load_atoms(dest)
    centroids = nodes.cluster_centroids(atoms)
    gateways = nodes.gateways_for(dest)
    cache = routing.RoutingCache(dest)
    client = routing.OSRMClient(offline=offline)
    stats = {"dest": dest, "filled": 0, "osrm_ok": 0, "fallback": 0,
             "skipped_short_leg": 0, "skipped_cached": 0, "requests": 0}

    # ---- 1+2. 质心全矩阵 + gateway↔质心：按 profile 分组，一次 table ----
    hub_points: list[tuple[float, float]] = []
    hub_labels: list[str] = []
    for name, c in sorted(centroids.items()):
        hub_points.append((c["lat"], c["lon"]))
        hub_labels.append(f"cluster:{name}")
    for gid, g in sorted(gateways.items()):
        hub_points.append((g["lat"], g["lon"]))
        hub_labels.append(f"gateway:{gid}")

    n_cent = len(centroids)
    hub_pairs_by_profile: dict[str, list[tuple[int, int]]] = {}
    for i, j in itertools.combinations(range(len(hub_points)), 2):
        # 全矩阵只算质心↔质心与 gateway↔质心；gateway↔gateway 对规划无意义
        if i >= n_cent and j >= n_cent:
            continue
        km = haversine_km(*hub_points[i], *hub_points[j])
        if km <= WALK_MAX_KM:
            stats["skipped_short_leg"] += 1
            continue
        profile = routing.profile_for_km(km)
        if cache.get(hub_points[i], hub_points[j], profile):
            stats["skipped_cached"] += 1
            continue
        hub_pairs_by_profile.setdefault(profile, []).append((i, j))

    for profile, pairs in sorted(hub_pairs_by_profile.items()):
        _fill_from_table(cache, client, hub_points, pairs, profile, stats)

    # ---- 3. 簇内 atom↔atom（>1.2km 的腿）----
    by_cluster: dict[str, list[dict]] = {}
    for a in atoms:
        by_cluster.setdefault(a.get("spatial", {}).get("cluster", "_unknown"), []).append(a)
    for cname, members in sorted(by_cluster.items()):
        pts = [(a["spatial"]["lat"], a["spatial"]["lon"]) for a in members]
        pairs_by_profile: dict[str, list[tuple[int, int]]] = {}
        for i, j in itertools.combinations(range(len(pts)), 2):
            km = haversine_km(*pts[i], *pts[j])
            if km <= WALK_MAX_KM:
                stats["skipped_short_leg"] += 1
                continue
            profile = routing.profile_for_km(km)
            if cache.get(pts[i], pts[j], profile):
                stats["skipped_cached"] += 1
                continue
            pairs_by_profile.setdefault(profile, []).append((i, j))
        for profile, pairs in sorted(pairs_by_profile.items()):
            _fill_from_table(cache, client, pts, pairs, profile, stats)

    cache.save()
    stats["requests"] = client.requests_made
    stats["cache"] = cache.stats()
    stats["network"] = ("offline-forced" if offline else
                        "ok" if client.available else
                        ("unavailable→haversine fallback" if client.available is False
                         else "unused(all cached)"))
    if not quiet:
        print(f"[{dest}] filled={stats['filled']} (osrm={stats['osrm_ok']}, "
              f"haversine_fallback={stats['fallback']}) "
              f"cached_skip={stats['skipped_cached']} short_leg_skip={stats['skipped_short_leg']} "
              f"requests={stats['requests']} network={stats['network']}")
        c = stats["cache"]
        print(f"[{dest}] cache total={c['total']} osrm={c['osrm']} "
              f"haversine_estimate={c['haversine_estimate']}")
    return stats


def compare(dest: str) -> None:
    """haversine vs OSRM 跨簇（质心↔质心）通行时间对比。纯读缓存，零网络。"""
    atoms = _load_atoms(dest)
    centroids = nodes.cluster_centroids(atoms)
    cache = routing.RoutingCache(dest)
    rows = []
    for n1, n2 in itertools.combinations(sorted(centroids), 2):
        a = (centroids[n1]["lat"], centroids[n1]["lon"])
        b = (centroids[n2]["lat"], centroids[n2]["lon"])
        hav, km, _ = TravelMatrix.estimate_minutes(a, b)
        hit = cache.get(a, b)
        osrm_min = hit["minutes"] if hit and hit["source"] == routing.SOURCE_OSM else None
        rows.append((n1, n2, round(km, 2), hav, osrm_min))
    osrm_rows = [r for r in rows if r[4] is not None]
    print(f"\n=== {dest} 跨簇质心对：haversine vs OSRM ===")
    print(f"{'pair':<44} {'km':>6} {'hav_min':>8} {'osrm_min':>9} {'delta':>7}")
    for n1, n2, km, hav, osrm_min in rows:
        o = f"{osrm_min:9.1f}" if osrm_min is not None else "      n/a"
        d = f"{osrm_min - hav:+7.1f}" if osrm_min is not None else "      -"
        print(f"{n1 + '↔' + n2:<44} {km:6.2f} {hav:8.1f} {o} {d}")
    if osrm_rows:
        avg_h = sum(r[3] for r in osrm_rows) / len(osrm_rows)
        avg_o = sum(r[4] for r in osrm_rows) / len(osrm_rows)
        print(f"平均（{len(osrm_rows)} 对有 OSRM 数据）：haversine {avg_h:.1f} min "
              f"→ OSRM {avg_o:.1f} min（Δ {avg_o - avg_h:+.1f} min, "
              f"{(avg_o - avg_h) / avg_h * 100:+.0f}%）")
    else:
        print("无 OSRM 数据可对比（全部回退或缓存为空）")


def main() -> None:
    ap = argparse.ArgumentParser(description="OSRM 通行矩阵预计算（BACKLOG #5）")
    ap.add_argument("--dest", help="只构建指定目的地；缺省全部")
    ap.add_argument("--offline", action="store_true",
                    help="不访问网络，全部用 haversine 兜底填充（source=haversine_estimate）")
    ap.add_argument("--compare", action="store_true",
                    help="构建后打印 haversine vs OSRM 跨簇对比")
    args = ap.parse_args()

    registry = _load_registry()
    dests = [args.dest] if args.dest else [d["id"] for d in registry["destinations"]]
    for dest in dests:
        if dest not in {d["id"] for d in registry["destinations"]}:
            print(f"未知目的地: {dest}", file=sys.stderr)
            sys.exit(2)
        build_dest(dest, offline=args.offline)
    if args.compare:
        for dest in dests:
            compare(dest)


if __name__ == "__main__":
    main()
