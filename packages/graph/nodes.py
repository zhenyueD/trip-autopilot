"""节点模型：gateway 内置表 + lodging_zone 派生（V1-BUILD §2 节点表）。

- gateway：门户坐标为确定性事实（OSM/公开数据），手工录入内置表，禁止模型编造。
- lodging_zone：由 cluster 派生 —— 中心性 = 原子数 / (1 + 距市中心 km)，
  取 top 3–5 作为可住宿区锚点；primary 为每天动线的起终点。
"""
from __future__ import annotations

import math

# ---- 门户内置表（坐标来源：OSM/公开地理数据，人工录入） ----
# kind: airport / rail；primary_airport 为骨架进/出默认门户
GATEWAYS: dict[str, dict[str, dict]] = {
    "osaka": {
        "kix": {
            "id": "osaka.gateway.kix", "name_zh": "关西国际机场", "kind": "airport",
            "lat": 34.4273, "lon": 135.2440, "primary_airport": True,
        },
        "itm": {
            "id": "osaka.gateway.itm", "name_zh": "大阪伊丹机场", "kind": "airport",
            "lat": 34.7855, "lon": 135.4382, "primary_airport": False,
        },
        "shin_osaka": {
            "id": "osaka.gateway.shin_osaka", "name_zh": "新大阪站", "kind": "rail",
            "lat": 34.7335, "lon": 135.5002, "primary_airport": False,
        },
        "jr_osaka": {
            "id": "osaka.gateway.jr_osaka", "name_zh": "JR 大阪站", "kind": "rail",
            "lat": 34.7024, "lon": 135.4959, "primary_airport": False,
        },
        "namba_sta": {
            "id": "osaka.gateway.namba", "name_zh": "难波站", "kind": "rail",
            "lat": 34.6671, "lon": 135.5009, "primary_airport": False,
        },
    },
    "bangkok": {
        "bkk": {
            "id": "bangkok.gateway.bkk", "name_zh": "素万那普机场", "kind": "airport",
            "lat": 13.6900, "lon": 100.7501, "primary_airport": True,
        },
        "dmk": {
            "id": "bangkok.gateway.dmk", "name_zh": "廊曼机场", "kind": "airport",
            "lat": 13.9126, "lon": 100.6068, "primary_airport": False,
        },
        "hua_lamphong": {
            "id": "bangkok.gateway.hua_lamphong", "name_zh": "华南蓬火车站", "kind": "rail",
            "lat": 13.7391, "lon": 100.5167, "primary_airport": False,
        },
    },
    "abudhabi": {
        # AUH 距市中心约 30km（近 Yas Island），是类似 KIX 的结构性通勤门户
        # 坐标：航站楼区（任务指定；OSM way/685739503 aerodrome 质心 24.4409,54.6485 旁证）
        "auh": {
            "id": "abudhabi.gateway.auh", "name_zh": "扎耶德国际机场", "kind": "airport",
            "lat": 24.4330, "lon": 54.6511, "primary_airport": True,
        },
        # 阿布扎比无轨道交通；Main Bus Terminal 是主要城际巴士枢纽
        # 坐标：OSM way/67145713（محطة الباصات الرئيسية أبو ظبي）
        "central_bus": {
            "id": "abudhabi.gateway.central_bus", "name_zh": "阿布扎比中央汽车站", "kind": "rail",
            "lat": 24.4706, "lon": 54.3766, "primary_airport": False,
        },
    },
}


def gateways_for(dest: str) -> dict[str, dict]:
    """某目的地的门户表；未知目的地返回空表（调用方负责兜底）。"""
    return GATEWAYS.get(dest, {})


def primary_gateway(dest: str) -> dict | None:
    """骨架进/出默认门户：primary_airport 优先，否则任一 airport，再否则任一门戶。"""
    gws = gateways_for(dest)
    if not gws:
        return None
    for g in gws.values():
        if g.get("primary_airport"):
            return g
    for g in gws.values():
        if g["kind"] == "airport":
            return g
    return next(iter(gws.values()))


def _centroid(atoms: list[dict]) -> tuple[float, float]:
    lats = [a["spatial"]["lat"] for a in atoms]
    lons = [a["spatial"]["lon"] for a in atoms]
    return sum(lats) / len(lats), sum(lons) / len(lons)


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def cluster_centroids(atoms: list[dict]) -> dict[str, dict]:
    """每个 cluster 的质心与原子数（确定性：均值坐标）。"""
    by_cluster: dict[str, list[dict]] = {}
    for a in atoms:
        by_cluster.setdefault(a.get("spatial", {}).get("cluster", "_unknown"), []).append(a)
    out: dict[str, dict] = {}
    for name, members in by_cluster.items():
        lat, lon = _centroid(members)
        out[name] = {"cluster": name, "lat": lat, "lon": lon, "atom_count": len(members)}
    return out


def derive_lodging_zones(atoms: list[dict], registry_entry: dict,
                         top_n: int = 5, min_n: int = 3) -> list[dict]:
    """住宿区锚点：中心性最高的 3–5 个 cluster（V1-BUILD §2）。

    中心性 = atom_count / (1 + 距目的地中心 km)。规模相近时取离市中心更近者。
    返回按中心性降序的列表；[0] 为 primary lodging zone（每日动线起终点）。
    """
    cents = cluster_centroids(atoms)
    clat = registry_entry.get("center_lat", 0.0)
    clon = registry_entry.get("center_lon", 0.0)
    scored = []
    for c in cents.values():
        dist = _km(c["lat"], c["lon"], clat, clon)
        centrality = c["atom_count"] / (1.0 + dist)
        scored.append((centrality, -dist, c["cluster"], c))
    # 确定性排序：中心性降序 → 距中心升序 → 名称字典序
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    n = max(min_n, min(top_n, len(scored)))
    zones = []
    for i, (centrality, neg_dist, name, c) in enumerate(scored[:n]):
        zones.append({
            "id": f"{registry_entry['id']}.lodging.{name}",
            "cluster": name,
            "name_zh": f"{name} 片区",
            "lat": c["lat"], "lon": c["lon"],
            "atom_count": c["atom_count"],
            "centrality": round(centrality, 4),
            "dist_to_center_km": round(-neg_dist, 2),
            "primary": i == 0,
        })
    return zones
