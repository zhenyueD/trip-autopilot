"""enrich.py —— 确定性启发式富化：OSM/Wikivoyage 标签 → schema 字段。

纪律（schema §3.7 铁律）：
  - 未知字段一律 null，不编造
  - name.zh 仅取 OSM name:zh；否则用 en/local 占位并 needs_llm_enrichment=true（禁止音译编造）
  - summary 启发式模板 → needs_llm_enrichment=true；有 Wikivoyage 真实描述的可降标记但仍需 LLM 精化

用法：python3 -m packages.atomizer.enrich [--dest ...]
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from .common import DATA_INTERIM, DESTINATIONS, today_iso

ALL_SEASONS = {"spring": 1.0, "summer": 1.0, "autumn": 1.0, "winter": 1.0}


# ---------------------------------------------------------------- categories
def categorize(c: dict) -> list[str]:
    tags, wv = c.get("tags") or {}, c.get("wv_type")
    # BACKLOG #7：定向抓取 hint 优先（day_trip / transport_experience）
    hint = tags.get("_atomizer_hint")
    if hint:
        return [hint]
    cats: list[str] = []
    t, h = tags.get("tourism"), tags.get("historic")
    leisure, am, shop = tags.get("leisure"), tags.get("amenity"), tags.get("shop")
    hw, attr = tags.get("highway"), tags.get("attraction")

    if t in ("museum", "gallery"):
        cats.append("art_museum")
    if t == "viewpoint":
        cats += ["viewpoint", "photo_spot"]
    if attr in ("ferris_wheel", "observation_wheel"):
        cats += ["transport_experience", "viewpoint", "photo_spot"]
    if t in ("zoo", "aquarium", "theme_park"):
        cats.append("activity")
    if t == "attraction":
        cats.append("sightseeing")
    if t == "artwork":
        cats.append("photo_spot")
    if h or am == "place_of_worship":
        cats.append("culture_history")
    if leisure in ("park", "garden"):
        cats.append("nature_park")
    if shop in ("mall", "department_store"):
        cats.append("shopping")
    if shop == "marketplace" or hw == "pedestrian":
        cats.append("market_street")
    if am in ("public_bath", "spa") or shop == "massage":
        cats.append("onsen_spa")
    if am in ("restaurant", "cafe", "food_court", "biergarten"):
        cats.append("food")
    if am in ("bar", "nightclub"):
        cats.append("nightlife")
    if not cats and wv:
        cats += {"see": ["sightseeing"], "do": ["activity"], "eat": ["food"],
                 "drink": ["nightlife"], "buy": ["shopping"]}.get(wv, ["sightseeing"])
    if not cats:
        cats = ["sightseeing"]
    # 去重保序，最多 3 个
    out = list(dict.fromkeys(cats))[:3]
    return out


# ---------------------------------------------------------------- emotion_tags
EMOTION_BY_CATEGORY = {
    "culture_history": ["cultural_deep", "healing"],
    "art_museum": ["cultural_deep", "quiet_escape"],
    "viewpoint": ["night_view", "photo", "romantic"],
    "nature_park": ["healing", "family_fun", "quiet_escape"],
    "shopping": ["lively_local", "family_fun"],
    "market_street": ["lively_local", "foodie", "bargain"],
    "food": ["foodie", "lively_local"],
    "nightlife": ["lively_local", "adventure"],
    "onsen_spa": ["healing", "luxury"],
    "activity": ["family_fun", "adventure"],
    "transport_experience": ["night_view", "photo", "romantic"],
    "photo_spot": ["photo", "iconic"],
    "sightseeing": ["iconic", "photo"],
    "day_trip": ["adventure", "quiet_escape"],
}


def emotion_tags(c: dict, cats: list[str]) -> list[str]:
    tags: list[str] = []
    for cat in cats[:2]:
        tags += EMOTION_BY_CATEGORY.get(cat, [])
    t = (c.get("tags") or {})
    # 修正：城堡/知名地标 → iconic；神社寺庙 → 怀旧+文化深潜
    if t.get("historic") in ("castle", "fort"):
        tags += ["iconic", "photo"]
    if t.get("amenity") == "place_of_worship":
        tags += ["cultural_deep", "nostalgic"]
    if (c.get("wikidata") or c.get("wikipedia")) and t.get("tourism") in ("attraction", "museum"):
        tags.append("iconic")
    out = list(dict.fromkeys(tags))
    if len(out) >= 2:
        return out[:5]
    # 兜底：validate 要求 2-5 个且应互不相同；旧实现 (out+["iconic"])[:5]
    # 在 out==["iconic"] 时产出重复值、out==[] 时只产 1 个 → FAIL
    for fallback in ("iconic", "photo", "quiet_escape"):
        if fallback not in out:
            out.append(fallback)
        if len(out) >= 2:
            break
    return out[:5]


# ---------------------------------------------------------------- 其余字段
DURATION_BY_CATEGORY = {
    "art_museum": 90, "nature_park": 90, "culture_history": 60, "viewpoint": 60,
    "shopping": 120, "market_street": 90, "food": 60, "nightlife": 120,
    "onsen_spa": 120, "activity": 180, "transport_experience": 45,
    "photo_spot": 30, "sightseeing": 90, "day_trip": 240,
}

BEST_TIME_BY_CATEGORY = {
    "viewpoint": ["sunset", "evening", "night"],
    "transport_experience": ["sunset", "evening", "night"],
    "nightlife": ["evening", "night"],
    "food": ["noon", "evening"],
    "market_street": ["morning", "noon", "afternoon"],
    "nature_park": ["morning", "afternoon"],
    "culture_history": ["morning", "afternoon"],
    "art_museum": ["morning", "afternoon"],
    "shopping": ["afternoon", "evening"],
    "onsen_spa": ["afternoon", "evening"],
    "photo_spot": ["sunset", "evening"],
    "sightseeing": ["morning", "afternoon"],
    "activity": ["morning", "afternoon"],
}

OUTDOOR_CATS = {"nature_park", "market_street", "transport_experience", "day_trip"}
INDOOR_CATS = {"art_museum", "shopping", "onsen_spa", "food"}


def weather_dependency(cats: list[str]) -> str:
    if cats[0] in OUTDOOR_CATS:
        return "outdoor_preferred"
    if cats[0] in ("viewpoint", "photo_spot"):
        return "outdoor_preferred"
    if cats[0] == "culture_history":
        return "outdoor_preferred"   # 寺院/城堡以户外参观为主
    if cats[0] in INDOOR_CATS or cats[0] == "nightlife":
        return "indoor_ok"
    return "indoor_ok"


# 目的地气候画像：season_fit 的季节修正（夏季极端高温是阿布扎比的结构性约束，
# 户外原子 summer 适配分大幅下调 —— 这是季节感知重组的核心展示场景）
DEST_CLIMATE = {
    "osaka":    {"summer_outdoor": 0.7, "shoulder": 1.0, "winter": 0.8},
    "bangkok":  {"summer_outdoor": 0.8, "shoulder": 1.0, "winter": 1.0},  # 全年炎热，季节差小
    "abudhabi": {"summer_outdoor": 0.15, "shoulder": 0.8, "winter": 1.0},  # 5–9 月 45°C+，户外近乎不可行
}


def season_fit(cats: list[str], dest: str = "osaka") -> dict[str, float]:
    climate = DEST_CLIMATE.get(dest, DEST_CLIMATE["osaka"])
    if cats[0] in ("nature_park", "photo_spot", "viewpoint", "transport_experience",
                   "market_street", "day_trip"):
        # 纯户外
        return {"spring": climate["shoulder"], "summer": climate["summer_outdoor"],
                "autumn": climate["shoulder"], "winter": climate["winter"]}
    if cats[0] in ("culture_history", "sightseeing"):
        # 半户外（寺院/城堡/地标以户外参观为主）
        return {"spring": climate["shoulder"], "summer": min(0.9, climate["summer_outdoor"] + 0.15),
                "autumn": climate["shoulder"], "winter": climate["winter"]}
    # 室内为主（博物馆/商场/餐饮/水疗/夜生活）：空调环境，全年可
    return dict(ALL_SEASONS)


def budget_tier(c: dict, cats: list[str]) -> int:
    t = c.get("tags") or {}
    if cats[0] in ("nature_park", "market_street", "photo_spot"):
        return 1
    if cats[0] == "culture_history":
        return 2 if (t.get("historic") in ("castle", "fort") or t.get("tourism")) else 1
    if cats[0] == "nightlife":
        return 3
    if cats[0] in ("shopping",):
        return 3
    if cats[0] in ("viewpoint", "transport_experience", "art_museum"):
        return 2
    if cats[0] == "onsen_spa":
        return 3
    if cats[0] == "activity":
        return 3
    if cats[0] == "food":
        return 2
    return 2


def physical_level(cats: list[str]) -> int:
    return {
        "nature_park": 2, "culture_history": 2, "sightseeing": 3, "market_street": 3,
        "art_museum": 1, "shopping": 2, "viewpoint": 1, "transport_experience": 1,
        "onsen_spa": 1, "food": 1, "nightlife": 2, "photo_spot": 2,
        "activity": 3, "day_trip": 4,
    }.get(cats[0], 2)


# schema v0.2：energy_cost 体力/认知消耗指数（1=可久坐休整 … 5=高强度）
ENERGY_BY_CATEGORY = {
    "onsen_spa": 1, "food": 1, "transport_experience": 1, "viewpoint": 2,
    "art_museum": 2, "nightlife": 2, "shopping": 3, "market_street": 3,
    "culture_history": 3, "sightseeing": 3, "nature_park": 3,
    "photo_spot": 2, "activity": 4, "day_trip": 5,
}


def energy_cost(c: dict, cats: list[str]) -> int:
    """启发式初标：类别基线 + 体力等级修正；LLM 精化覆盖。"""
    base = ENERGY_BY_CATEGORY.get(cats[0], 3)
    t = c.get("tags") or {}
    if t.get("tourism") in ("theme_park", "zoo"):
        base = 5
    pl = physical_level(cats)
    return max(1, min(5, round((base + pl) / 2)))


def suitable_for(c: dict, cats: list[str]) -> list[str]:
    t = c.get("tags") or {}
    base = ["solo", "couple", "family", "group_friends"]
    if t.get("tourism") in ("zoo", "aquarium", "theme_park") or cats[0] == "activity":
        base.append("kids")
    if cats[0] in ("art_museum", "onsen_spa", "viewpoint", "culture_history"):
        base.append("elderly")
    return list(dict.fromkeys(base))


def crowd_level(c: dict) -> int:
    if c.get("wikipedia"):
        return 4
    if c.get("wikidata"):
        return 4
    if c.get("wv_sources") or c["coord_source"] == "wikivoyage":
        return 3
    return 2


SUMMARY_TMPL = {
    "culture_history": "参观{n}，感受当地历史与信仰文化",
    "art_museum": "在{n}看展，了解这座城市的美术与记忆",
    "viewpoint": "登上{n}俯瞰城市全景，黄昏与夜景尤其出色",
    "nature_park": "在{n}散步放松，体验城市中的自然",
    "shopping": "在{n}逛街购物，从伴手礼到潮流单品",
    "market_street": "逛{n}，边逛边吃感受本地烟火气",
    "food": "在{n}品尝本地美食",
    "nightlife": "在{n}体验本地夜生活",
    "onsen_spa": "在{n}泡汤放松，洗去旅途疲惫",
    "activity": "在{n}参与体验活动，适合安排半天时间",
    "transport_experience": "乘坐{n}，换个角度看城市",
    "photo_spot": "在{n}拍照打卡",
    "sightseeing": "游览{n}，打卡代表性景观",
    "day_trip": "以{n}为目的地的近郊一日 excursion",
}


def build_summary(c: dict, cats: list[str], name_zh: str) -> tuple[str, bool]:
    """返回 (summary, is_heuristic)。Wikivoyage 有真实描述时用它（截断 60 字）。"""
    if c.get("wv_content"):
        s = re.sub(r"\s+", " ", c["wv_content"]).strip()
        # 过短或纯本地文字（泰文/日文名）不算有效描述
        if len(s) >= 20 and re.search(r"[a-zA-Z]{4}", s):
            return s[:60], True   # 机器截断，仍标记待 LLM 精化
    tmpl = SUMMARY_TMPL.get(cats[0], SUMMARY_TMPL["sightseeing"])
    return tmpl.format(n=name_zh)[:60], True


# ---------------------------------------------------------------- 主流程
def enrich_dest(dest: str) -> None:
    src = DATA_INTERIM / f"clustered_{dest}.json"
    blob = json.loads(src.read_text(encoding="utf-8"))
    cfg = DESTINATIONS[dest]
    price_field = cfg["price_field"]
    atoms = []
    for c in blob["candidates"]:
        cats = categorize(c)
        name_en = c.get("name_en")
        name_local = c.get("name_local") or name_en
        # name.zh 铁律：只用真实 name:zh（多值取第一个），否则占位 + 标记
        raw_zh = (c.get("name_zh") or "").split(";")[0].strip() or None
        zh_is_real = bool(raw_zh)
        name_zh = raw_zh or name_en or name_local
        summary, _ = build_summary(c, cats, name_zh)
        needs_llm = True  # V0 全部待 LLM 精化（summary/emotion 均为启发式）
        emo = emotion_tags(c, cats)
        atom = {
            "schema_version": "0.2",
            "name": {"zh": name_zh, "en": name_en, "local": name_local},
            "destination": dest,
            "categories": cats,
            "summary": summary,
            "time_structure": {
                "typical_duration_min": DURATION_BY_CATEGORY.get(cats[0], 90),
                "best_time_of_day": BEST_TIME_BY_CATEGORY.get(cats[0], ["any"]),
                "open_time_windows": None,
                "season_fit": season_fit(cats, dest),
                "weather_dependency": weather_dependency(cats),
            },
            "spatial": {
                "lat": round(c["lat"], 6),
                "lon": round(c["lon"], 6),
                "cluster": c["cluster"],
                "walk_minutes_to_cluster_center": c["walk_minutes_to_cluster_center"],
            },
            "constraints": {
                "booking_required": "recommended" if (c.get("tags") or {}).get("tourism") == "theme_park" else "none",
                "booking_lead_days": 0,
                "physical_level": physical_level(cats),
                "energy_cost": energy_cost(c, cats),
                "budget_tier": budget_tier(c, cats),
                price_field: None,
                "crowd_level": crowd_level(c),
                "suitable_for": suitable_for(c, cats),
                "unsuitable_for": [],
            },
            "emotion_tags": emo,
            "supply_links": [],
            "facts": {
                "source": list(dict.fromkeys(c["src_ids"] + c.get("wv_sources", []))),
                "last_verified": today_iso(),
                "needs_llm_enrichment": needs_llm,
            },
            "_name_zh_real": zh_is_real,          # emit 后剔除
            "_slug_base": c.get("name_en") or c.get("name_local") or name_zh,
        }
        atoms.append(atom)
    out = {"destination": dest, "clusters": blob.get("clusters", []), "atoms": atoms}
    path = DATA_INTERIM / f"enriched_{dest}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[enrich] {dest}: {len(atoms)} 原子已富化 → {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", choices=list(DESTINATIONS), default=None)
    args = ap.parse_args()
    for d in ([args.dest] if args.dest else list(DESTINATIONS)):
        enrich_dest(d)


if __name__ == "__main__":
    sys.exit(main())
