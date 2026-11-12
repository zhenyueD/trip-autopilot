"""validate.py —— schema §6 质量门校验 + 质量报告。

门槛：
  - 每目的地 ≥150 原子（大阪目标 ≥200）
  - 覆盖 ≥10 个 categories
  - 每 cluster ≥5 原子
  - 100% facts.source 非空
  - OSM 坐标覆盖率 ≥90%（facts.source[0] 以 "osm:" 开头）
  - 无 null lat/lon；id 全库唯一；name.zh 非空

用法：python3 -m packages.atomizer.validate
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter

from .common import DATA_ATOMS, DESTINATIONS

CATEGORIES_ALL = {
    "sightseeing", "food", "market_street", "shopping", "viewpoint",
    "culture_history", "art_museum", "nature_park", "onsen_spa", "nightlife",
    "activity", "photo_spot", "transport_experience", "day_trip",
}
EMOTIONS_ALL = {
    "healing", "lively_local", "photo", "foodie", "romantic", "family_fun",
    "nostalgic", "luxury", "adventure", "quiet_escape", "cultural_deep",
    "night_view", "bargain", "iconic", "hidden_gem",
}
BEST_TIME_ALL = {"early_morning", "morning", "noon", "afternoon", "sunset",
                 "evening", "night", "any"}
WEATHER_ALL = {"outdoor_only", "outdoor_preferred", "indoor_ok", "indoor_only"}
SUITABLE_ALL = {"solo", "couple", "family", "elderly", "kids",
                "group_friends", "business"}
# schema v0.4 tips.applies_to：persona ∪ 时段 ∪ 季节
APPLIES_TO_ALL = (SUITABLE_ALL | BEST_TIME_ALL
                  | {"spring", "summer", "autumn", "winter"})

# summary 长度合同（docs/SUPPLY-ATOM-SCHEMA.md §字段表："≤60字"）
SUMMARY_MAX = 60


def validate_payload(dest: str, payload: dict, global_ids: set[str],
                     quiet: bool = False) -> tuple[bool, list[str]]:
    """对已载入内存的 payload 做字段级校验 + 质量门。供 validate_dest 与
    各写回路径（llm_enrich/blend_energy/dedupe_existing）落盘前内联校验复用。
    quiet=True 时只计算不打印（内联校验用）。"""
    atoms = payload["atoms"]
    errors: list[str] = []
    warnings: list[str] = []

    # ---- 字段级校验 ----
    cat_counter: Counter = Counter()
    cluster_counter: Counter = Counter()
    emo_counter: Counter = Counter()
    osm_coord = 0
    llm_pending = 0
    for a in atoms:
        aid = a.get("id", "<no-id>")
        if aid in global_ids:
            errors.append(f"重复 id: {aid}")
        global_ids.add(aid)
        sp = a.get("spatial") or {}
        if sp.get("lat") is None or sp.get("lon") is None:
            errors.append(f"{aid}: null 坐标")
        if not (a.get("facts") or {}).get("source"):
            errors.append(f"{aid}: facts.source 为空")
        elif str(a["facts"]["source"][0]).startswith("osm:"):
            osm_coord += 1
        if not ((a.get("name") or {}).get("zh")):
            errors.append(f"{aid}: name.zh 为空")
        s_len = len(a.get("summary") or "")
        if s_len > SUMMARY_MAX:
            errors.append(f"{aid}: summary 超长 {s_len} > {SUMMARY_MAX}（schema 合同 ≤60字）")
        for cat in a.get("categories", []):
            if cat not in CATEGORIES_ALL:
                errors.append(f"{aid}: 非法 category '{cat}'")
            cat_counter[cat] += 1
        for e in a.get("emotion_tags", []):
            if e not in EMOTIONS_ALL:
                errors.append(f"{aid}: 非法 emotion_tag '{e}'")
            emo_counter[e] += 1
        ts = a.get("time_structure") or {}
        for b in ts.get("best_time_of_day", []):
            if b not in BEST_TIME_ALL:
                errors.append(f"{aid}: 非法 best_time_of_day '{b}'")
        if ts.get("weather_dependency") not in WEATHER_ALL:
            errors.append(f"{aid}: 非法 weather_dependency")
        if ts.get("open_time_windows") is not None:
            errors.append(f"{aid}: open_time_windows 必须为 null（V0）")
        dur = ts.get("typical_duration_min")
        if not isinstance(dur, int) or not (30 <= dur <= 240):
            errors.append(f"{aid}: typical_duration_min 越界: {dur}")
        cs = a.get("constraints") or {}
        if cs.get("booking_required") not in ("none", "recommended", "required"):
            errors.append(f"{aid}: 非法 booking_required")
        if not (1 <= (cs.get("physical_level") or 0) <= 5):
            errors.append(f"{aid}: physical_level 越界")
        ec = cs.get("energy_cost")
        if not isinstance(ec, int) or not (1 <= ec <= 5):
            errors.append(f"{aid}: energy_cost 缺失或越界（schema v0.2 必填 int 1-5）: {ec}")
        if not (1 <= (cs.get("budget_tier") or 0) <= 4):
            errors.append(f"{aid}: budget_tier 越界")
        if not (1 <= (cs.get("crowd_level") or 0) <= 5):
            errors.append(f"{aid}: crowd_level 越界")
        for s in cs.get("suitable_for", []):
            if s not in SUITABLE_ALL:
                errors.append(f"{aid}: 非法 suitable_for '{s}'")
        if not (2 <= len(a.get("emotion_tags", [])) <= 5):
            errors.append(f"{aid}: emotion_tags 数量 {len(a.get('emotion_tags', []))} 不在 2–5")
        cl = sp.get("cluster")
        if not cl:
            errors.append(f"{aid}: 缺 cluster")
        cluster_counter[cl] += 1
        if a["facts"].get("needs_llm_enrichment"):
            llm_pending += 1
        le = a.get("llm_enrichment")
        if le is not None:
            for k in ("model", "model_date", "prompt_version", "confidence", "needs_review"):
                if k not in le:
                    errors.append(f"{aid}: llm_enrichment 缺字段 {k}")
        # schema v0.3：media 可选；有 image_url 时必须 https + 域名白名单 + 来源/授权齐全
        media = a.get("media")
        if media is not None:
            iu = media.get("image_url")
            if iu is not None:
                if not (isinstance(iu, str) and re.match(
                        r"^https://([a-z0-9-]+\.)*(wikimedia\.org|wikipedia\.org|mapillary\.com)/", iu)):
                    errors.append(f"{aid}: media.image_url 非 https 或不在 "
                                  f"wikimedia/wikipedia/mapillary 白名单: {iu}")
                if media.get("image_source") not in ("wikimedia_commons", "wikipedia", "mapillary"):
                    errors.append(f"{aid}: 有 image_url 但 image_source 非法: "
                                  f"{media.get('image_source')}")
                if not media.get("image_license"):
                    errors.append(f"{aid}: 有 image_url 但缺 image_license")
        # schema v0.4：tips 可选；每条必须可溯源、在有效期内、长度合规
        tips = a.get("tips")
        if tips is not None:
            if not isinstance(tips, list):
                errors.append(f"{aid}: tips 必须是数组")
            else:
                import datetime as _dt
                for ti, tip in enumerate(tips):
                    tag = f"{aid}.tips[{ti}]"
                    tz = tip.get("tip_zh")
                    if not (isinstance(tz, str) and tz.strip()):
                        errors.append(f"{tag}: tip_zh 为空")
                    elif len(tz) > 80:
                        errors.append(f"{tag}: tip_zh 超长 {len(tz)} > 80")
                    src = tip.get("source") or {}
                    su = src.get("url")
                    if not (isinstance(su, str) and re.match(
                            r"^https://([a-z0-9-]+\.)*wikivoyage\.org/", su)):
                        errors.append(f"{tag}: source.url 必填且限 wikivoyage.org 域名: {su}")
                    if src.get("type") not in ("wikivoyage",):
                        errors.append(f"{tag}: source.type 非法: {src.get('type')}")
                    try:
                        conf = float(tip.get("confidence"))
                        if not (0.0 <= conf <= 1.0):
                            raise ValueError
                    except (TypeError, ValueError):
                        errors.append(f"{tag}: confidence 缺失或越界: {tip.get('confidence')}")
                    for dk in ("extracted_at", "expires_at"):
                        dv = tip.get(dk)
                        try:
                            _dt.date.fromisoformat(str(dv))
                        except (TypeError, ValueError):
                            errors.append(f"{tag}: {dk} 非法日期: {dv}")
                    try:
                        if _dt.date.fromisoformat(str(tip.get("expires_at"))) <= _dt.date.today():
                            errors.append(f"{tag}: expires_at 不在未来（已过期）: {tip.get('expires_at')}")
                    except (TypeError, ValueError):
                        pass  # 上面已报非法日期
                    for t in tip.get("applies_to") or []:
                        if t not in APPLIES_TO_ALL:
                            errors.append(f"{tag}: 非法 applies_to 标签 '{t}'")

    # ---- 质量门 ----
    n = len(atoms)
    gate_min = 150
    target = DESTINATIONS[dest]["target"]
    gates = []
    gates.append((f"原子数 ≥ {gate_min}（目标 {target}）", n >= gate_min, f"{n}"))
    gates.append(("categories 覆盖 ≥ 10", len(cat_counter) >= 10, f"{len(cat_counter)}"))
    small_clusters = {k: v for k, v in cluster_counter.items() if v < 5}
    gates.append(("每 cluster ≥ 5 原子", not small_clusters, str(small_clusters or "OK")))
    src_pct = 100.0 * sum(1 for a in atoms if a["facts"]["source"]) / max(n, 1)
    gates.append(("facts.source 覆盖 100%", src_pct == 100.0, f"{src_pct:.1f}%"))
    osm_pct = 100.0 * osm_coord / max(n, 1)
    gates.append(("OSM 坐标覆盖率 ≥ 90%", osm_pct >= 90.0, f"{osm_pct:.1f}%"))

    if not quiet:
        print(f"\n===== {dest.upper()} 质量报告 =====")
        print(f"原子数: {n} | categories: {len(cat_counter)} | clusters: {len(cluster_counter)}")
        needs_review_n = sum(1 for a in atoms
                             if (a.get("llm_enrichment") or {}).get("needs_review"))
        print(f"待 LLM 精化: {llm_pending} ({100.0*llm_pending/max(n,1):.0f}%)"
              f" | 其中 LLM 已跑但需人工复核: {needs_review_n}")
        print("categories 分布:")
        for k, v in cat_counter.most_common():
            print(f"  {k:22s} {v}")
        print("clusters 分布:")
        for k, v in cluster_counter.most_common():
            print(f"  {k:22s} {v}")
        print("质量门:")
    all_pass = True
    for name, ok, detail in gates:
        if not quiet:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name} → {detail}")
        all_pass &= ok
    if errors:
        all_pass = False
        if not quiet:
            print(f"字段级错误 {len(errors)} 条（前 20）:")
            for e in errors[:20]:
                print(f"  ✗ {e}")
    if not quiet:
        for w in warnings:
            print(f"  ⚠ {w}")
    return all_pass, errors


def validate_dest(dest: str, global_ids: set[str]) -> tuple[bool, list[str]]:
    path = DATA_ATOMS / f"{dest}.json"
    if not path.exists():
        return False, [f"[{dest}] 缺少 {path}"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    return validate_payload(dest, payload, global_ids)


def write_atoms_atomic(dest: str, payload: dict) -> None:
    """原子性写回 data/atoms/{dest}.json：先写临时文件，跑内联校验
    （validate_payload，独立 id 集 —— 跨目的地唯一性由全量 validate 把关），
    通过才 os.replace；失败则保留旧文件并抛错。"""
    import os
    import tempfile
    ok, errors = validate_payload(dest, payload, set(), quiet=True)
    if not ok:
        raise ValueError(f"[{dest}] 落盘前内联校验未通过（{len(errors)} 条）: "
                         + "; ".join(errors[:5]))
    fd, tmp = tempfile.mkstemp(dir=str(DATA_ATOMS), prefix=f".{dest}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=1))
        os.replace(tmp, DATA_ATOMS / f"{dest}.json")
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main() -> int:
    global_ids: set[str] = set()
    ok_all = True
    for dest in DESTINATIONS:
        ok, _ = validate_dest(dest, global_ids)
        ok_all &= ok
    print(f"\n===== 总体: {'全部通过 ✅' if ok_all else '存在未达标项 ❌'} =====")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
