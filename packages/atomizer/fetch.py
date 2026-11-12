"""fetch.py —— 抓取 OSM Overpass + Wikivoyage，解析为候选 POI。

用法：python3 -m packages.atomizer.fetch [--force] [--dest osaka|bangkok]

产物：
  data/raw/overpass_{dest}.json          OSM 原始响应（缓存，重跑不再请求）
  data/raw/wikivoyage_{dest}.json        Wikivoyage 各分区页面 wikitext（缓存）
  data/interim/candidates_{dest}.json    去重合并后的候选 POI + place 节点
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
import sys

from .common import (
    DATA_INTERIM, DATA_RAW, DESTINATIONS, OVERPASS_ENDPOINTS, OVERPASS_UA,
    WIKIVOYAGE_API, haversine_km, make_session, norm_name, slugify,
)

SESSION = make_session()            # Wikivoyage 用（浏览器 UA）
OVERPASS_SESSION = make_session(OVERPASS_UA)  # Overpass 用（项目 UA）

# Overpass 查询：覆盖 schema §4.1 各 category 所需的真实供给。
# 拆成多个小查询逐块抓取（单块缓存），避免大查询超时。
QUERY_CHUNKS = [
    ("tourism", 'nwr["tourism"~"^(attraction|museum|gallery|viewpoint|zoo|theme_park|aquarium|artwork)$"]["name"]({bbox});'),
    ("historic", 'nwr["historic"~"^(castle|monument|memorial|ruins|archaeological_site|shrine|temple|fort)$"]["name"]({bbox});'),
    ("park", 'nwr["leisure"~"^(park|garden)$"]["name"]({bbox});'),
    ("worship", 'nwr["amenity"="place_of_worship"]["name"]({bbox});'),
    ("mall", 'nwr["shop"~"^(mall|department_store|marketplace)$"]["name"]({bbox});'),
    ("spa", 'nwr["amenity"~"^(public_bath|spa)$"]["name"]({bbox});\n  nwr["shop"="massage"]["name"]({bbox});'),
    ("pedestrian", 'nwr["highway"="pedestrian"]["name"]({bbox});'),
    ("wheel", 'nwr["attraction"~"^(ferris_wheel|observation_wheel)$"]["name"]({bbox});'),
    ("famous_food", 'nwr["amenity"~"^(restaurant|cafe|bar|nightclub|food_court|biergarten)$"]["name"]["wikidata"]({bbox});\n  nwr["amenity"~"^(restaurant|cafe|bar|nightclub|food_court|biergarten)$"]["name"]["wikipedia"]({bbox});'),
    # BACKLOG #6：餐饮放宽 —— 带 cuisine 的餐厅/咖啡馆/快餐 + 全部具名酒吧/夜店
    ("food", 'nwr["amenity"~"^(restaurant|cafe|fast_food|food_court)$"]["name"]["cuisine"]({bbox});'),
    ("nightlife", 'nwr["amenity"~"^(bar|nightclub|pub)$"]["name"]({bbox});'),
    ("places", 'node["place"~"^(suburb|neighbourhood|quarter)$"]["name"]({bbox});'),
]
OVERPASS_QUERY_TMPL = "[out:json][timeout:120];\n(\n  {body}\n);\nout center tags;"

# BACKLOG #7：day_trip / transport_experience 定向抓取（真实 OSM 实体，hint 驱动分类）
# 每项: (chunk名, bbox覆盖或None(用目的地bbox), overpass body, hint)
EXTRA_QUERIES = {
    "osaka": [
        ("transport", None,
         'nwr["attraction"~"^(big_wheel|ferris_wheel|observation_wheel)$"]["name"]({bbox});\n'
         '  nwr["tourism"="attraction"]["name"~"クルーズ|cruise|サンタマリア|観光船",i]({bbox});\n'
         '  nwr["route"="ferry"]["name"]({bbox});\n'
         '  nwr["name"~"^(とんぼりリバークルーズ|HEP ?FIVE|天保山大観覧車)$",i]({bbox});',
         "transport_experience"),
        ("daytrip_kyoto", (34.95, 135.65, 35.05, 135.80),
         'nwr["tourism"~"^(attraction|museum)$"]["name"]["wikidata"]({bbox});\n  nwr["historic"~"^(castle|temple|shrine)$"]["name"]["wikidata"]({bbox});\n  nwr["amenity"="place_of_worship"]["name"]["wikidata"]({bbox});',
         "day_trip"),
        ("daytrip_nara", (34.64, 135.78, 34.72, 135.87),
         'nwr["tourism"~"^(attraction|museum)$"]["name"]["wikidata"]({bbox});\n  nwr["leisure"="park"]["name"]["wikidata"]({bbox});\n  nwr["amenity"="place_of_worship"]["name"]["wikidata"]({bbox});',
         "day_trip"),
    ],
    "bangkok": [
        ("transport", None,
         'nwr["tourism"="boat"]["name"]({bbox});\n'
         '  nwr["route"="ferry"]["name"]({bbox});\n'
         '  nwr["name"~"Chao Phraya.*(Cruise|Express)|dinner cruise|long.?tail boat",i]["tourism"]({bbox});\n'
         '  nwr["amenity"="ferry_terminal"]["name"]({bbox});',
         "transport_experience"),
        ("daytrip_maeklong", (13.36, 99.95, 13.50, 100.10),
         'nwr["tourism"~"^(attraction|market)$"]["name"]({bbox});\n  nwr["shop"="marketplace"]["name"]({bbox});\n  nwr["name"~"Maeklong|Mae Klong|แม่กลอง|Railway Market",i]({bbox});',
         "day_trip"),
        ("daytrip_amphawa", (13.40, 99.90, 13.48, 100.02),
         'nwr["tourism"~"^(attraction|market)$"]["name"]({bbox});\n  nwr["name"~"Amphawa|อัมพวา|Floating Market",i]({bbox});',
         "day_trip"),
    ],
    "abudhabi": [
        ("transport", None,
         'nwr["attraction"~"^(big_wheel|ferris_wheel|observation_wheel)$"]["name"]({bbox});\n'
         '  nwr["name"~"^(Marina Eye|Ain Dhabi)$",i]({bbox});',
         "transport_experience"),
        ("daytrip_desert", (24.00, 54.30, 24.60, 55.30),
         'nwr["name"~"Desert Safari|Rub al Khali|Empty Quarter|Liwa",i]["tourism"]({bbox});\n  nwr["tourism"~"^(attraction|theme_park)$"]["name"]["wikidata"](24.30,54.55,24.55,54.70);',
         "day_trip"),
    ],
}

# 同名合并半径（米）
MERGE_SAME_NAME_KM = 0.05
# 同名二次合并半径（node+way 质心可能差几百米）
MERGE_SAME_NAME_KM_WIDE = 0.4
# 同 wikidata 合并半径；超出视为误标，保留标签更多者
MERGE_WIKIDATA_KM = 1.0
# Wikivoyage listing 与 OSM 候选合并半径（米）
WV_MATCH_KM = 0.06

# 明显无体验价值的泛名/商号（便利店、公司注册名等）
BLACKLIST_NAME = re.compile(
    r"(seven.?eleven|7.?eleven|familymart|family mart|lawson|ministop|circle ?k|7-11"
    r"|l\.?l\.?c\.?$|\bllc\b|\bltd\b|\bgmbh\b|trading\b|stationery|stationary"
    r"|office supplies|technology|solutions|ind\. mat|\bco\.$|\best\.$)",
    re.I,
)


# ---------------------------------------------------------------- Overpass
def _post_chunk(dest: str, chunk_name: str, bbox: str, body_tmpl: str,
                force: bool, hint: str | None = None) -> list[dict]:
    """抓单个块（带缓存），hint 注入 tags._atomizer_hint 驱动后续分类。"""
    cache = DATA_RAW / f"overpass_{dest}_{chunk_name}.json"
    if cache.exists() and not force:
        data = json.loads(cache.read_text(encoding="utf-8"))
        print(f"[fetch] {dest}/{chunk_name}: 缓存 {len(data.get('elements', []))} elements")
    else:
        query = OVERPASS_QUERY_TMPL.format(body=body_tmpl.format(bbox=bbox))
        data, last_err = None, None
        for endpoint in OVERPASS_ENDPOINTS:
            try:
                print(f"[fetch] {dest}/{chunk_name}: POST {endpoint} ...", flush=True)
                r = OVERPASS_SESSION.post(endpoint, data={"data": query}, timeout=110)
                r.raise_for_status()
                data = r.json()
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                print(f"[fetch] {dest}/{chunk_name}: {endpoint} 失败: {type(exc).__name__}: {exc}")
        if data is None:
            print(f"[fetch] {dest}/{chunk_name}: 全部端点失败（{last_err}），此块记为空")
            data = {"elements": []}
        cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        print(f"[fetch] {dest}/{chunk_name}: {len(data.get('elements', []))} elements")
    elements = data.get("elements", [])
    for el in elements:
        tags = el.setdefault("tags", {})
        tags["_atomizer_chunk"] = chunk_name
        if hint:
            tags["_atomizer_hint"] = hint
    return elements


def fetch_overpass(dest: str, force: bool = False) -> dict:
    """分块抓取 Overpass，每块独立缓存（data/raw/overpass_{dest}_{chunk}.json）。"""
    s, w, n, e = DESTINATIONS[dest]["bbox"]
    bbox = f"{s},{w},{n},{e}"
    all_elements: list[dict] = []
    for chunk_name, body_tmpl in QUERY_CHUNKS:
        all_elements += _post_chunk(dest, chunk_name, bbox, body_tmpl, force)
    for chunk_name, bbox_override, body_tmpl, hint in EXTRA_QUERIES.get(dest, []):
        if bbox_override:
            bbox2 = f"{bbox_override[0]},{bbox_override[1]},{bbox_override[2]},{bbox_override[3]}"
        else:
            bbox2 = bbox
        all_elements += _post_chunk(dest, f"x_{chunk_name}", bbox2, body_tmpl, force, hint=hint)
    print(f"[fetch] {dest}: 合计 {len(all_elements)} elements")
    return {"elements": all_elements}


def _keep_hint(dst: dict, src: dict) -> None:
    """合并时保留定向抓取的 hint/chunk 标记；hint 来自哪个定向块，chunk 就记哪个块。"""
    dt, st = dst.setdefault("tags", {}), src.get("tags") or {}
    if not dt.get("_atomizer_hint") and st.get("_atomizer_hint"):
        dt["_atomizer_hint"] = st["_atomizer_hint"]
        if st.get("_atomizer_chunk"):
            dt["_atomizer_chunk"] = st["_atomizer_chunk"]


def parse_osm_candidates(data: dict) -> tuple[list[dict], list[dict]]:
    """返回 (candidates, place_nodes)。去重：同名 50m 内合并，优先有 wikidata / way。"""
    cands, places = [], []
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        name = tags.get("name") or tags.get("name:en")
        if not name:
            continue
        lat, lon = el.get("lat"), el.get("lon")
        if lat is None and "center" in el:
            lat, lon = el["center"].get("lat"), el["center"].get("lon")
        if lat is None or lon is None:
            continue
        rec = {
            "src_ids": [f"osm:{el['type']}/{el['id']}"],
            "coord_source": "osm",
            "lat": float(lat), "lon": float(lon),
            "name_local": tags.get("name"),
            "name_en": tags.get("name:en"),
            "name_zh": tags.get("name:zh"),
            "wikidata": tags.get("wikidata"),
            "wikipedia": tags.get("wikipedia"),
            "tags": tags,
            "wv_sources": [], "wv_type": None, "wv_content": None,
        }
        if tags.get("place") in ("suburb", "neighbourhood", "quarter") and not any(
            k in tags for k in ("tourism", "historic", "leisure", "amenity", "shop", "highway")
        ):
            places.append(rec)
            continue
        if BLACKLIST_NAME.search(name) or BLACKLIST_NAME.search(tags.get("name:en") or ""):
            continue
        cands.append(rec)

    # 同名 50m 合并（node+way 重复）。按名分组，避免 O(n²)
    cands.sort(key=lambda c: (norm_name(c["name_local"] or c["name_en"]), c["lat"]))
    groups: dict[str, list[dict]] = {}
    for c in cands:
        groups.setdefault(norm_name(c["name_local"] or c["name_en"]), []).append(c)
    merged: list[dict] = []
    for _name, members in groups.items():
        group_merged: list[dict] = []
        for c in members:
            hit = None
            for m in group_merged:
                if haversine_km(m["lat"], m["lon"], c["lat"], c["lon"]) <= MERGE_SAME_NAME_KM:
                    hit = m
                    break
            if hit is None:
                group_merged.append(c)
                continue
            # 合并：优先有 wikidata 的记录作为载体
            hit["src_ids"] = list(dict.fromkeys(hit["src_ids"] + c["src_ids"]))
            for k in ("name_en", "name_zh", "name_local", "wikidata", "wikipedia"):
                if not hit.get(k) and c.get(k):
                    hit[k] = c[k]
            _keep_hint(hit, c)
            if c.get("wikidata") and not hit.get("wikidata"):
                hit["lat"], hit["lon"] = c["lat"], c["lon"]
        merged.extend(group_merged)

    # 二次去重：同 wikidata 跨距离合并（node/way/relation 同实体），
    # 以及同名 400m 内合并（大小清真寺/卢浮宫等 node+way 质心分离场景）
    by_wd: dict[str, dict] = {}
    final: list[dict] = []
    dropped = 0
    for c in merged:
        wd = c.get("wikidata")
        if wd and wd in by_wd:
            keep, drop = by_wd[wd], c
            # 保留标签更全者（wikipedia > name_zh > name_en）
            score = lambda x: bool(x.get("wikipedia")) + bool(x.get("name_zh")) + bool(x.get("name_en")) + len(x["src_ids"])
            if score(drop) > score(keep):
                keep, drop = drop, keep
                final.remove(by_wd[wd])
                final.append(keep)
                by_wd[wd] = keep
            keep["src_ids"] = list(dict.fromkeys(keep["src_ids"] + drop["src_ids"]))
            for k in ("name_en", "name_zh", "name_local", "wikipedia"):
                if not keep.get(k) and drop.get(k):
                    keep[k] = drop[k]
            _keep_hint(keep, drop)
            dropped += 1
            continue
        if wd:
            by_wd[wd] = c
        final.append(c)
    # 同名宽半径合并（按名分组 + 网格索引，避免 O(n²)）
    merged2: list[dict] = []
    name_groups: dict[str, list[dict]] = {}
    for c in final:
        name_groups.setdefault(norm_name(c["name_local"] or c["name_en"]), []).append(c)
    for nc, members in name_groups.items():
        for c in members:
            hit = None
            # 仅在同名/子串组内找
            candidates_pool = []
            for nm_key, pool in name_groups.items():
                # 子串匹配备查池；单字名（如"車"）禁止子串匹配，否则会吞掉"HEP FIVE観覧車"
                if nm_key == nc or (nm_key and nc and min(len(nm_key), len(nc)) >= 2
                                    and (nm_key in nc or nc in nm_key)):
                    candidates_pool.extend(pool)
            for m in candidates_pool:
                if m is c or m.get("_absorbed"):
                    continue
                if haversine_km(m["lat"], m["lon"], c["lat"], c["lon"]) <= MERGE_SAME_NAME_KM_WIDE:
                    hit = m
                    break
            if hit is None or hit is c:
                merged2.append(c)
                continue
            hit["src_ids"] = list(dict.fromkeys(hit["src_ids"] + c["src_ids"]))
            for k in ("name_en", "name_zh", "name_local", "wikidata", "wikipedia"):
                if not hit.get(k) and c.get(k):
                    hit[k] = c[k]
            _keep_hint(hit, c)
            c["_absorbed"] = True
            dropped += 1
    merged2 = [c for c in merged2 if not c.get("_absorbed")]
    for c in merged2:
        c.pop("_absorbed", None)
    print(f"[parse] OSM candidates: {len(cands)} → 合并后 {len(merged2)} "
          f"(二次去重 -{dropped}); place 节点 {len(places)}")
    return merged2, places


# ---------------------------------------------------------------- Wikivoyage
def wv_api(params: dict) -> dict:
    p = {"format": "json", "formatversion": "2"}
    p.update(params)
    r = SESSION.get(WIKIVOYAGE_API, params=p, timeout=60)
    r.raise_for_status()
    return r.json()


def discover_district_pages(dest: str) -> list[str]:
    cfg = DESTINATIONS[dest]
    pages = [cfg["wv_root"]]
    try:
        data = wv_api({"action": "query", "list": "prefixsearch",
                       "pssearch": cfg["wv_prefix"], "pslimit": 100})
        for item in data.get("query", {}).get("prefixsearch", []):
            title = item.get("title", "")
            # 只要一层子页面（分区页），跳过 Osaka/Kita/something 之类
            if title.count("/") == 1:
                pages.append(title)
    except Exception as exc:  # noqa: BLE001
        print(f"[fetch] {dest}: Wikivoyage prefixsearch 失败: {exc}（仅使用根页面）")
    # 去重保序，限量
    seen, out = set(), []
    for p in pages:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out[:20]


def fetch_wikivoyage(dest: str, force: bool = False) -> dict[str, str]:
    """返回 {page_title: wikitext}"""
    cache = DATA_RAW / f"wikivoyage_{dest}.json"
    if cache.exists() and not force:
        print(f"[fetch] {dest}: 使用缓存 {cache.name}")
        return json.loads(cache.read_text(encoding="utf-8"))
    pages = discover_district_pages(dest)
    result: dict[str, str] = {}
    for title in pages:
        try:
            data = wv_api({
                "action": "query", "prop": "revisions", "rvprop": "content",
                "rvslots": "main", "redirects": "1", "titles": title,
            })
            for pg in data.get("query", {}).get("pages", []):
                revs = pg.get("revisions") or []
                if revs:
                    content = revs[0]["slots"]["main"]["content"]
                    result[title] = content
                    print(f"[fetch] {dest}: Wikivoyage '{title}' {len(content)} chars")
        except Exception as exc:  # noqa: BLE001
            print(f"[fetch] {dest}: Wikivoyage '{title}' 失败: {exc}")
    cache.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


# ------------------------------------------------- Wikivoyage listing 解析
LISTING_TYPES = ("see", "do", "eat", "drink", "buy", "listing")


def _extract_template(text: str, start: int) -> tuple[str, int] | None:
    """从 start('{{' 位置) 抽取平衡括号的模板，返回 (模板文本, 结束位置)。"""
    depth, i = 0, start
    while i < len(text) - 1:
        two = text[i:i + 2]
        if two == "{{":
            depth += 1
            i += 2
            continue
        if two == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return text[start:i], i
            continue
        i += 1
    return None


def _split_params(body: str) -> dict[str, str]:
    """切分模板顶层 | 参数（忽略嵌套模板内的 |）。"""
    parts, depth, cur = [], 0, ""
    i = 0
    while i < len(body):
        two = body[i:i + 2]
        if two in ("{{", "[["):
            depth += 1
            cur += two
            i += 2
            continue
        if two in ("}}", "]]"):
            depth = max(0, depth - 1)
            cur += two
            i += 2
            continue
        if body[i] == "|" and depth == 0:
            parts.append(cur)
            cur = ""
            i += 1
            continue
        cur += body[i]
        i += 1
    parts.append(cur)
    params = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.strip().lower()] = v.strip()
    return params


def _clean_wiki(text: str) -> str:
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)  # 去嵌套模板（一轮足够多数情况）
    text = re.sub(r"\[\[([^|\]]*\|)?([^\]]*)\]\]", r"\2", text)  # [[a|b]]→b
    text = re.sub(r"\[https?://[^\s\]]+\s*([^\]]*)\]", r"\1", text)
    text = re.sub(r"'''?", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_wv_listings(pages: dict[str, str]) -> list[dict]:
    listings = []
    for page, wikitext in pages.items():
        lower = wikitext.lower()
        i = 0
        while True:
            j = lower.find("{{", i)
            if j == -1:
                break
            m = re.match(r"\{\{\s*(see|do|eat|drink|buy|listing)\b", lower[j:])
            if not m:
                i = j + 2
                continue
            ext = _extract_template(wikitext, j)
            if not ext:
                break
            tmpl, i = ext
            ltype = m.group(1)
            body = tmpl[m.end():-2]   # 去掉 "{{see" 前缀与结尾 "}}"
            params = _split_params(body)
            name = _clean_wiki(params.get("name", ""))
            if not name:
                continue
            lat, lon = params.get("lat", ""), params.get("long", "")
            try:
                lat_f, lon_f = float(lat), float(lon)
            except (TypeError, ValueError):
                lat_f = lon_f = None
            content = _clean_wiki(params.get("content", "") or params.get("alt", ""))
            listings.append({
                "page": page, "type": ltype, "name": name,
                "lat": lat_f, "lon": lon_f, "content": content,
            })
    print(f"[parse] Wikivoyage listings: {len(listings)} 条（含坐标 "
          f"{sum(1 for l in listings if l['lat'] is not None)} 条）")
    return listings


def merge_wikivoyage(cands: list[dict], listings: list[dict]) -> None:
    """Wikivoyage listing 与 OSM 候选按 同名 或 60m 内 合并；无匹配的带坐标 listing 成为新候选。"""
    matched = 0
    for li in listings:
        src = f"wikivoyage:{li['page']}#{slugify(li['name']) or norm_name(li['name'])[:20]}"
        target = None
        nli = norm_name(li["name"])
        if nli:
            for c in cands:
                if norm_name(c["name_en"]) == nli or norm_name(c["name_local"]) == nli:
                    target = c
                    break
        if target is None and li["lat"] is not None:
            best, bestd = None, WV_MATCH_KM
            for c in cands:
                d = haversine_km(c["lat"], c["lon"], li["lat"], li["lon"])
                if d <= bestd:
                    # 60m 内要求名字至少有共同 token，避免误合并
                    toks_c = set(norm_name(c["name_en"] or "").split()) | set(norm_name(c["name_local"] or ""))
                    toks_l = set(norm_name(li["name"]).split())
                    if toks_c & toks_l or norm_name(c["name_en"]) == nli:
                        best, bestd = c, d
            target = best
        if target is not None:
            target["wv_sources"].append(src)
            if li["content"] and not target.get("wv_content"):
                target["wv_content"] = li["content"]
            if not target.get("wv_type"):
                target["wv_type"] = li["type"]
            matched += 1
        elif li["lat"] is not None:
            cands.append({
                "src_ids": [src],
                "coord_source": "wikivoyage",
                "lat": li["lat"], "lon": li["lon"],
                "name_local": li["name"], "name_en": li["name"], "name_zh": None,
                "wikidata": None, "wikipedia": None,
                "tags": {}, "wv_sources": [], "wv_type": li["type"],
                "wv_content": li["content"] or None,
            })
    print(f"[merge] Wikivoyage 合并进 OSM: {matched} 条；新增独立候选: "
          f"{sum(1 for c in cands if c['coord_source'] == 'wikivoyage')}")


# ---------------------------------------------------------------- 主流程
def notability(c: dict) -> int:
    s = 0
    if c.get("wikidata"):
        s += 3
    if c.get("wikipedia"):
        s += 2
    if c.get("wv_sources") or c["coord_source"] == "wikivoyage":
        s += 2
    if c.get("name_en"):
        s += 1
    return s


def food_signal(c: dict) -> int:
    """餐饮信号强度（BACKLOG #6）：米其林/wikidata/官网/菜系/Wikivoyage 优先。"""
    t = c.get("tags") or {}
    s = notability(c)
    if any(k.startswith("michelin") or k == "stars" for k in t):
        s += 5
    if t.get("website") or t.get("contact:website") or t.get("url"):
        s += 1
    if t.get("cuisine"):
        s += 1
    return s


# 餐饮/夜生活按地理网格（≈2km）配额 + 菜系多样性，防 500 家同质居酒屋
FOOD_CELL_DEG = 0.018
FOOD_CELL_CAP = 15
FOOD_SAME_CUISINE_CAP = 5


def _food_cell(lat: float, lon: float) -> tuple[int, int]:
    return (int(lat / FOOD_CELL_DEG), int(lon / FOOD_CELL_DEG))


def cap_food_candidates(cands: list[dict], cat_key: str = "_primary_cat") -> list[dict]:
    """food/nightlife 候选：网格 × 菜系多样性限量，返回 (入选, 落选)。"""
    food = [c for c in cands if c[cat_key] in ("food", "nightlife")]
    rest = [c for c in cands if c[cat_key] not in ("food", "nightlife")]
    food.sort(key=food_signal, reverse=True)
    cell_count: Counter = Counter()
    cuisine_count: Counter = Counter()
    kept, dropped = [], []
    for c in food:
        cell = _food_cell(c["lat"], c["lon"])
        cuisine = (c.get("tags") or {}).get("cuisine", "").split(";")[0] or "_unknown"
        key = (cell, cuisine)
        if cell_count[cell] >= FOOD_CELL_CAP or cuisine_count[key] >= FOOD_SAME_CUISINE_CAP:
            dropped.append(c)
            continue
        cell_count[cell] += 1
        cuisine_count[key] += 1
        kept.append(c)
    return rest + kept, dropped


def run_dest(dest: str, force: bool = False) -> None:
    osm = fetch_overpass(dest, force=force)
    cands, places = parse_osm_candidates(osm)
    wv_pages = fetch_wikivoyage(dest, force=force)
    listings = parse_wv_listings(wv_pages)
    merge_wikivoyage(cands, listings)

    # 质量过滤与限量：notability 优先 + 品类配额（防止寺庙/神社挤掉餐饮夜生活）
    from .enrich import categorize  # 延迟导入避免循环
    cfg = DESTINATIONS[dest]
    cap = cfg["cap"]
    # BACKLOG #7：剔除城际长途渡轮（route=ferry 关系的质心常落在市外海上，
    # 如 上海-大阪/大阪-釜山/さんふらわあ）；市内渡船（天保山渡船等）质心在 bbox 内不受影响。
    # 另外按名剔除"（城市－城市）"括号航路（如 名門大洋フェリー (大阪－新門司)，质心恰在港内）
    import re as _re
    INTERCITY_FERRY = _re.compile(r'[（(][^）)]*[－~〜～–—-][^）)]*[）)]')
    s, w, n, e = cfg["bbox"]
    M = 0.12  # bbox 外扩容差（度）
    before_ferry = len(cands)
    def _is_intercity_ferry(c: dict) -> bool:
        if (c.get("tags") or {}).get("route") != "ferry":
            return False
        name = (c.get("name_local") or "") + " " + (c.get("name_en") or "")
        if INTERCITY_FERRY.search(name):
            return True
        return not (s - M <= c["lat"] <= n + M and w - M <= c["lon"] <= e + M)
    cands = [c for c in cands if not _is_intercity_ferry(c)]
    if len(cands) < before_ferry:
        print(f"[filter] {dest}: 剔除城际长途渡轮 {before_ferry - len(cands)} 条")
    # 主品类配额：餐饮/夜生活/市集/购物/温泉/体验全保留；寺庙神社与公园限量
    UNLIMITED = {"food", "nightlife", "market_street", "shopping", "onsen_spa",
                 "activity", "viewpoint", "transport_experience", "photo_spot",
                 "art_museum", "day_trip"}
    CAPPED_QUOTA = {"culture_history": int(cap * 0.30),
                    "nature_park": int(cap * 0.15),
                    "sightseeing": int(cap * 0.20)}
    for c in cands:
        c["_primary_cat"] = categorize(c)[0]
        c["_score"] = notability(c)
    # BACKLOG #6：餐饮/夜生活先做网格×菜系限量，再进总量 cap
    cands, food_dropped = cap_food_candidates(cands)
    print(f"[filter] {dest}: 餐饮网格限量剔除 {len(food_dropped)} 条")
    cands.sort(key=lambda c: (c["_score"] + (2 if c["_primary_cat"] in
              ("food", "nightlife") else 0)), reverse=True)
    # BACKLOG #7：定向抓取的 hint 候选保底入库，day_trip 按块限量（每块最多 8 条，
    # 只要最知名的；京都块一次能捞几百个 wikidata 寺庙，不限制会挤爆 cap）
    hinted = [c for c in cands if (c.get("tags") or {}).get("_atomizer_hint")]
    hinted.sort(key=lambda c: c["_score"], reverse=True)
    # 每块限量：day_trip 8（京都块一次能捞几百个 wikidata 寺庙）；
    # transport 20（曼谷 ferry_terminal 一捞 170+ 个码头，全留会挤爆 cap）
    CHUNK_CAP = {"day_trip": 8, "transport_experience": 20}
    hinted_kept: list[dict] = []
    dt_count: Counter = Counter()
    for c in hinted:
        cat = c["_primary_cat"]
        if cat in CHUNK_CAP:
            chunk = (c.get("tags") or {}).get("_atomizer_chunk", "_")
            if dt_count[chunk] >= CHUNK_CAP[cat]:
                continue
            dt_count[chunk] += 1
        hinted_kept.append(c)
    hinted_all_ids = {id(c) for c in hinted}       # 含被限掉的 day_trip
    hinted_ids = {id(c) for c in hinted_kept}
    kept: list[dict] = list(hinted_kept)
    capped_count: Counter = Counter()
    wv_only_quota = int(cap * 0.08)   # schema §6：OSM 坐标覆盖率 ≥90%，留安全余量
    wv_only_count = 0
    # BACKLOG #6：food/nightlife 保底配额，避免被高分寺庙挤掉
    FOOD_KEEP_CAP = int(cap * 0.25)
    food_kept = 0
    for c in cands:
        if id(c) in hinted_all_ids:
            continue
        if c["coord_source"] == "wikivoyage":
            if wv_only_count >= wv_only_quota:
                continue
            wv_only_count += 1
        cat = c["_primary_cat"]
        if cat in ("food", "nightlife"):
            if food_kept >= FOOD_KEEP_CAP:
                continue
            food_kept += 1
            kept.append(c)
        elif cat in UNLIMITED:
            kept.append(c)
        elif capped_count[cat] < CAPPED_QUOTA.get(cat, cap):
            capped_count[cat] += 1
            kept.append(c)
        if len(kept) >= cap:
            break
    print(f"[filter] {dest}: hint 保底 {len(hinted_kept)} 条（按块限量 {dict(dt_count)}）；"
          f"food/nightlife 保底 {food_kept} 条")
    for c in kept:
        c.pop("_primary_cat", None)
        c.pop("_score", None)
    print(f"[filter] {dest}: 候选 {len(cands)} → 保留 {len(kept)} "
          f"(cap={cap}; culture_history 配额内 {capped_count.get('culture_history', 0)})")

    out = {"destination": dest, "candidates": kept, "places": places}
    path = DATA_INTERIM / f"candidates_{dest}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[done] {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略缓存重新抓取")
    ap.add_argument("--dest", choices=list(DESTINATIONS), default=None)
    args = ap.parse_args()
    dests = [args.dest] if args.dest else list(DESTINATIONS)
    for d in dests:
        run_dest(d, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
