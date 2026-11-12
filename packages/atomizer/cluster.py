"""cluster.py —— 地理聚类（Wikivoyage 分区锚点驱动）。

策略：
  1. 从 Wikivoyage 各分区页的 listing 坐标计算「片区锚点」（真实数据，非编造）。
     如 Osaka/Minami → namba，Bangkok/Rattanakosin → rattanakosin。
  2. 每个原子归入最近锚点（≤2.8km），离群原子再 DBSCAN 地理聚类，
     用最近 OSM place 节点命名。
  3. 直径 > 2.2km 的簇递归切分（schema §3.3：类内步行 ≤25min）。
  4. 小簇（<5 原子）并入最近簇；最终每簇 ≥5 原子（schema §6）。

用法：python3 -m packages.atomizer.cluster [--dest ...]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter

import numpy as np
from sklearn.cluster import DBSCAN

from .common import DATA_INTERIM, DATA_RAW, DESTINATIONS, haversine_km, slugify
from .fetch import parse_wv_listings

EARTH_R = 6371.0088
ANCHOR_ASSIGN_KM = 2.8          # 归入片区锚点的最大距离
EPS_KM = 1.0                    # 离群原子 DBSCAN 半径
MAX_CLUSTER_DIAMETER_KM = 2.2   # 类内任意两点步行 ≤25min 的近似
MIN_CLUSTER_SIZE = 5
MIN_ANCHOR_LISTINGS = 3         # 分区页至少这么多含坐标 listing 才成为锚点

# Wikivoyage 分区页 → 规范片区 slug
ANCHOR_ALIASES = {
    "osaka": {
        "minami": "namba",
        "kita": "umeda",
        "tennoji": "tennoji",
        "osaka_castle": "osaka_castle",
        "bay_area": "bay_area",
        "north": "shin_osaka",
        "south": "sumiyoshi",
        "central": "hommachi",
        "hirano": "hirano",
    },
    "bangkok": {
        "rattanakosin": "rattanakosin",
        "khao_san_road": "banglamphu",
        "yaowarat_and_phahurat": "yaowarat",
        "yaowarat_and_phahurat_tour": "yaowarat",
        "siam_square": "siam",
        "silom": "silom",
        "sukhumvit": "sukhumvit",
        "dusit": "dusit",
        "thonburi": "thonburi",
        "pratunam": "pratunam",
        "ratchadaphisek": "ratchadaphisek",
        "phahonyothin": "chatuchak",
        "ramkhamhaeng": "ramkhamhaeng",
        "downtown": None,  # 根页重定向，跳过
    },
}


def _centroid(points: np.ndarray) -> tuple[float, float]:
    return float(points[:, 0].mean()), float(points[:, 1].mean())


def _diameter_km(points: np.ndarray) -> float:
    if len(points) < 2:
        return 0.0
    rad = np.radians(points)
    dmax = 0.0
    step = max(1, len(rad) // 60)  # 抽样，避免大簇 O(n²) 太慢
    idxs = list(range(0, len(rad), step))
    for i in idxs:
        dlat = rad[:, 0] - rad[i, 0]
        dlon = rad[:, 1] - rad[i, 1]
        a = np.sin(dlat / 2) ** 2 + np.cos(rad[i, 0]) * np.cos(rad[:, 0]) * np.sin(dlon / 2) ** 2
        d = 2 * EARTH_R * np.arcsin(np.sqrt(np.minimum(a, 1.0)))
        dmax = max(dmax, float(d.max()))
    return dmax


def build_anchors(dest: str) -> list[dict]:
    """从 Wikivoyage 缓存构建片区锚点。"""
    path = DATA_RAW / f"wikivoyage_{dest}.json"
    if not path.exists():
        return []
    pages = json.loads(path.read_text(encoding="utf-8"))
    root = DESTINATIONS[dest]["wv_root"]
    listings = parse_wv_listings(pages)
    by_page: dict[str, list[dict]] = {}
    for li in listings:
        if li["lat"] is not None:
            by_page.setdefault(li["page"], []).append(li)
    anchors = []
    aliases = ANCHOR_ALIASES.get(dest, {})
    for page, items in by_page.items():
        if page == root or "/" not in page:
            continue
        if len(items) < MIN_ANCHOR_LISTINGS:
            continue
        sub = slugify(page.split("/", 1)[1])
        slug = aliases.get(sub, sub)
        if not slug:
            continue
        lat = sum(i["lat"] for i in items) / len(items)
        lon = sum(i["lon"] for i in items) / len(items)
        anchors.append({"slug": slug, "lat": lat, "lon": lon, "page": page,
                        "n_listings": len(items)})
    print(f"[cluster] {dest}: {len(anchors)} 个 Wikivoyage 片区锚点: "
          f"{[a['slug'] for a in anchors]}")
    return anchors


def _split_recursive(members: list[int], pts: np.ndarray, eps_km: float,
                     depth: int = 0) -> list[list[int]]:
    """超直径大簇用 KMeans 二分（DBSCAN 在密集链式区域切不动）。"""
    from sklearn.cluster import KMeans
    if len(members) < 8 or _diameter_km(pts[members]) <= MAX_CLUSTER_DIAMETER_KM or depth > 4:
        return [members]
    xy = pts[members]
    km = KMeans(n_clusters=2, n_init=4, random_state=42).fit(xy)
    g0 = [m for m, lab in zip(members, km.labels_) if lab == 0]
    g1 = [m for m, lab in zip(members, km.labels_) if lab == 1]
    if not g0 or not g1:
        return [members]
    out: list[list[int]] = []
    for g in (g0, g1):
        out.extend(_split_recursive(g, pts, eps_km * 0.7, depth + 1))
    return out


def _load_place_nodes(dest: str) -> list[dict]:
    nodes: list[dict] = []
    for chunk in ("places", "neighbourhood"):
        path = DATA_RAW / f"overpass_{dest}_{chunk}.json"
        if not path.exists():
            continue
        for el in json.loads(path.read_text(encoding="utf-8")).get("elements", []):
            tags = el.get("tags") or {}
            if el.get("lat") is None or not tags.get("name"):
                continue
            nodes.append({"lat": el["lat"], "lon": el["lon"],
                          "place": tags.get("place"),
                          "name_en": tags.get("name:en"), "name": tags.get("name")})
    return nodes


def _name_outlier_cluster(c_lat, c_lon, places, members) -> str:
    best, best_score = None, None
    for p in places:
        d = haversine_km(c_lat, c_lon, p["lat"], p["lon"])
        if d > 2.5:
            continue
        score = d + (0.5 if p["place"] == "suburb" else 0.0) + (0.3 if not p["name_en"] else 0.0)
        if best_score is None or score < best_score:
            best, best_score = p, score
    if best is not None:
        slug = slugify(best.get("name_en")) or slugify(best.get("name"))
        if slug:
            return slug
    famous = [m for m in members if m.get("wikidata") or m.get("wikipedia")]
    if famous:
        slug = slugify(famous[0].get("name_en") or famous[0].get("name_local"))
        if slug:
            return slug
    return "outskirts"


def cluster_dest(dest: str) -> None:
    blob = json.loads((DATA_INTERIM / f"candidates_{dest}.json").read_text(encoding="utf-8"))
    cands = blob["candidates"]
    if not cands:
        return
    anchors = build_anchors(dest)
    places = _load_place_nodes(dest)
    pts = np.array([[c["lat"], c["lon"]] for c in cands])

    # 1) 锚点归属
    groups: dict[str, list[int]] = {}
    outliers: list[int] = []
    for i, c in enumerate(cands):
        best, bestd = None, ANCHOR_ASSIGN_KM
        for a in anchors:
            d = haversine_km(c["lat"], c["lon"], a["lat"], a["lon"])
            if d < bestd:
                best, bestd = a["slug"], d
        if best:
            groups.setdefault(best, []).append(i)
        else:
            outliers.append(i)
    print(f"[cluster] {dest}: 锚点归属 {sum(len(v) for v in groups.values())}, 离群 {len(outliers)}")

    # 2) 离群原子 DBSCAN
    if outliers:
        rad = np.radians(pts[outliers])
        labels = DBSCAN(eps=EPS_KM / EARTH_R, min_samples=2, metric="haversine").fit_predict(rad)
        sub: dict[int, list[int]] = {}
        for idx, lab in zip(outliers, labels):
            sub.setdefault(int(lab), []).append(idx)
        noise = sub.pop(-1, [])
        for lab, members in sub.items():
            c_lat, c_lon = _centroid(pts[members])
            slug = _name_outlier_cluster(c_lat, c_lon, places, [cands[i] for i in members])
            groups.setdefault(slug, []).extend(members)
        for idx in noise:  # 单点噪声归入最近任意组
            if groups:
                best = min(groups, key=lambda s: haversine_km(
                    pts[idx][0], pts[idx][1], *_centroid(pts[groups[s]])))
                groups[best].append(idx)

    # 3) 巨簇切分（保持片区名，加方位后缀）
    refined: list[tuple[str, list[int]]] = []
    for slug, members in groups.items():
        pieces = _split_recursive(members, pts, 0.7)
        if len(pieces) == 1:
            refined.append((slug, pieces[0]))
        else:
            for k, piece in enumerate(pieces, 1):
                refined.append((slug if k == 1 else f"{slug}_{k}", piece))

    # 4) 迭代稳定化：切超直径大簇 → 并小簇，直到两类都不存在（或轮数上限）
    MERGE_MAX_DIAMETER_KM = 3.2
    def dia(g): return _diameter_km(pts[g])
    for _round in range(6):
        # 4a) 切分超直径簇
        again: list[tuple[str, list[int]]] = []
        split_happened = False
        for slug, members in refined:
            if len(members) >= 8 and dia(members) > MAX_CLUSTER_DIAMETER_KM:
                pieces = _split_recursive(members, pts, 0.7)
                if len(pieces) > 1:
                    split_happened = True
                    for k2, piece in enumerate(pieces, 1):
                        again.append((slug if k2 == 1 else f"{slug}_{k2}", piece))
                    continue
            again.append((slug, members))
        refined = again
        # 4b) 并小簇：优先并入能保持直径上限的最近簇
        merge_happened = False
        while True:
            small_idx = [k for k, g in enumerate(refined) if len(g[1]) < MIN_CLUSTER_SIZE]
            if not small_idx:
                break
            k = small_idx[0]
            slug, members = refined.pop(k)
            if not refined:
                refined.append((slug, members))
                break
            c_lat, c_lon = _centroid(pts[members])
            targets = sorted(refined, key=lambda other: haversine_km(
                c_lat, c_lon, *_centroid(pts[other[1]])))
            chosen = None
            for t in targets:
                if len(t[1]) < MIN_CLUSTER_SIZE or dia(t[1] + members) <= MERGE_MAX_DIAMETER_KM:
                    chosen = t
                    break
            if chosen is None:
                chosen = targets[0]
            chosen[1].extend(members)
            merge_happened = True
        if not split_happened and not merge_happened:
            break

    # 5) 同名簇合并（切分后被并回的）
    merged: dict[str, list[int]] = {}
    for slug, members in refined:
        merged.setdefault(slug, []).extend(members)

    # 5b) 簇名清理：去掉迭代产生的深层 _2_2 后缀，同基名重新编号
    by_base: dict[str, list[list[int]]] = {}
    for slug, members in merged.items():
        base = re.sub(r"(_\d+)+$", "", slug)
        by_base.setdefault(base, []).append(members)
    renamed: dict[str, list[int]] = {}
    for base, groups_ in by_base.items():
        groups_.sort(key=len, reverse=True)
        for k, g in enumerate(groups_, 1):
            renamed[base if k == 1 else f"{base}_{k}"] = g
    merged = renamed

    # 汇总
    cluster_of: dict[int, str] = {}
    infos = []
    for slug, members in sorted(merged.items(), key=lambda kv: -len(kv[1])):
        c_lat, c_lon = _centroid(pts[members])
        for idx in members:
            cluster_of[idx] = slug
        infos.append({"slug": slug, "size": len(members),
                      "center": [round(c_lat, 6), round(c_lon, 6)],
                      "diameter_km": round(_diameter_km(pts[members]), 2)})
    for i, c in enumerate(cands):
        slug = cluster_of[i]
        c["cluster"] = slug
        info = next(ci for ci in infos if ci["slug"] == slug)
        d = haversine_km(c["lat"], c["lon"], info["center"][0], info["center"][1])
        c["walk_minutes_to_cluster_center"] = int(round(d * 1.3 / 5.0 * 60))

    out = {"destination": dest, "candidates": cands, "clusters": infos}
    path = DATA_INTERIM / f"clustered_{dest}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[cluster] {dest}: {len(cands)} 原子 → {len(infos)} 簇:")
    for ci in infos:
        print(f"    {ci['slug']:22s} n={ci['size']:3d} 直径={ci['diameter_km']}km")
    print(f"[done] {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", choices=list(DESTINATIONS), default=None)
    args = ap.parse_args()
    for d in ([args.dest] if args.dest else list(DESTINATIONS)):
        cluster_dest(d)


if __name__ == "__main__":
    sys.exit(main())
