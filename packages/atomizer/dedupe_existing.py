"""BACKLOG #3：存量原子一次性去重。

规则（只合并高置信重复，绝不丢信息）：
1. 簇内同名合并：规范化名（去空白/标点/变音符）相同且在同一个 cluster。
2. 跨簇近距同名合并：规范化名相同且坐标距离 ≤ 50m。
合并策略：保留信息更全的原子（facts.source 更多 > summary 更长 > 有真实中文名），
facts.source 取并集。坐标/类别等其余字段沿用被保留原子。
用法: python3 -m packages.atomizer.dedupe_existing [--dest osaka] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

DATA_ATOMS = Path(__file__).resolve().parents[2] / "data" / "atoms"
SAME_COORD_M = 50.0


def norm_name(n: str | None) -> str:
    n = unicodedata.normalize("NFKD", n or "")
    n = "".join(ch for ch in n if not unicodedata.combining(ch))
    return re.sub(r"[\s\-‐‑‒–—―・･()（）\[\]【】'’]", "", n.lower())


def haversine_m(a: dict, b: dict) -> float:
    from math import asin, cos, radians, sin, sqrt
    la1, lo1 = radians(a["spatial"]["lat"]), radians(a["spatial"]["lon"])
    la2, lo2 = radians(b["spatial"]["lat"]), radians(b["spatial"]["lon"])
    h = sin((la2 - la1) / 2) ** 2 + cos(la1) * cos(la2) * sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * asin(sqrt(h))


def richness(a: dict) -> tuple:
    """越大信息越全。"""
    return (len(a["facts"]["source"]),
            len(a.get("summary") or ""),
            a["name"].get("zh") not in (None, "", a["name"].get("en")))


def merge_into(resident: dict, other: dict) -> None:
    """把 other 合并进 resident（resident 必须是结果列表中的在列对象，就地更新）。
    表达层字段取信息更全者，facts.source 取并集。"""
    srcs = list(dict.fromkeys(resident["facts"]["source"] + other["facts"]["source"]))
    richer = other if richness(other) > richness(resident) else resident
    content = dict(richer)          # 浅拷贝，避免 richer is resident 时 clear 自毁
    resident.clear()
    resident.update(content)
    resident["facts"]["source"] = srcs


def dedupe(dest: str, dry_run: bool) -> tuple[int, int, list[str]]:
    path = DATA_ATOMS / f"{dest}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    atoms = payload["atoms"]
    before = len(atoms)
    log: list[str] = []

    # 1) 簇内同名 + 400m 距离护栏（空名字不入索引，避免空 key 互相覆盖/误合并）。
    #    距离护栏防止小簇并入后误并同名连锁分店（如 2.6km 外的两家 Burger King）；
    #    400m 与 fetch 的 MERGE_SAME_NAME_KM_WIDE 一致（覆盖 node+way 质心分离）
    by_cluster: dict[tuple, dict] = {}
    out: list[dict] = []
    for a in atoms:
        key = (a["spatial"]["cluster"], norm_name(a["name"]["en"] or a["name"]["local"]))
        if not key[1]:
            out.append(a)
            continue
        hit = by_cluster.get(key)
        if hit is not None and haversine_m(hit, a) <= 400.0:
            merge_into(hit, a)   # 就地并入在列载体，不丢对象
            log.append(f"簇内合并 [{key[0]}] {a['name']['en'] or a['name']['local']}")
        else:
            by_cluster[key] = a
            out.append(a)

    # 2) 跨簇 50m 内同名。b 并入 a 后标 absorbed，最后统一过滤——
    # 与迭代顺序无关（修复旧实现：keep 载体交换后重复 append / 丢载体的控制流 bug）
    by_name: dict[str, list[dict]] = defaultdict(list)
    for a in out:
        k = norm_name(a["name"]["en"] or a["name"]["local"])
        if k:
            by_name[k].append(a)
    absorbed: set[int] = set()
    for a in out:
        if id(a) in absorbed:
            continue
        k = norm_name(a["name"]["en"] or a["name"]["local"])
        for b in by_name.get(k, []):
            if b is a or id(b) in absorbed:
                continue
            if haversine_m(a, b) <= SAME_COORD_M:
                merge_into(a, b)
                absorbed.add(id(b))
                log.append(f"跨簇合并 [{a['spatial']['cluster']}~{b['spatial']['cluster']}] "
                           f"{b['name']['en'] or b['name']['local']}")
    final = [a for a in out if id(a) not in absorbed]

    # 3) 去重后跌破 5 原子的簇并入最近簇（保持"每簇 ≥ 5"质量门）
    counts: dict[str, int] = defaultdict(int)
    for a in final:
        counts[a["spatial"]["cluster"]] += 1
    small = {c for c, n in counts.items() if n < 5}
    if small:
        centroids: dict[str, tuple[float, float]] = {}
        by_cl: dict[str, list[dict]] = defaultdict(list)
        for a in final:
            by_cl[a["spatial"]["cluster"]].append(a)
        for cl, members in by_cl.items():
            centroids[cl] = (sum(m["spatial"]["lat"] for m in members) / len(members),
                             sum(m["spatial"]["lon"] for m in members) / len(members))
        for cl in small:
            members = by_cl[cl]
            la, lo = centroids[cl]
            target = min((c for c in centroids if c != cl),
                         key=lambda c: haversine_m(
                             {"spatial": {"lat": la, "lon": lo}},
                             {"spatial": {"lat": centroids[c][0], "lon": centroids[c][1]}}))
            for a in members:
                a["spatial"]["cluster"] = target
                d_km = haversine_m(a, {"spatial": {"lat": centroids[target][0],
                                                   "lon": centroids[target][1]}}) / 1000
                a["spatial"]["walk_minutes_to_cluster_center"] = int(round(d_km * 1.3 / 5.0 * 60))
            log.append(f"小簇并入 [{cl}] → [{target}]（{len(members)} 原子）")

    payload["atoms"] = final
    payload["atom_count"] = len(final)
    if not dry_run:
        from .validate import write_atoms_atomic
        write_atoms_atomic(dest, payload)
    return before, len(final), log


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", action="append")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    dests = args.dest or ["osaka", "bangkok", "abudhabi"]
    for d in dests:
        before, after, log = dedupe(d, args.dry_run)
        print(f"[dedupe] {d}: {before} → {after}（-{before - after}）")
        for line in log:
            print(f"    {line}")


if __name__ == "__main__":
    main()
