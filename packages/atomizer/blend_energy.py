"""一次性脚本：融合 LLM 能量分与启发式能量分。

背景：全量 LLM 精化后 energy_cost 分布压缩在 1-2（60% 原子=2），
solver 的 persona（带爸妈=低强度）失去区分度，验收用例回归。
融合规则：final = round(0.5 * llm + 0.5 * heuristic)，LLM 判断与
启发式类别基线各占一半，保住类别间区分度。heuristic 从 candidates_{dest}.json
按名匹配重算；LLM 原始值从 data/cache/llm_enrich/{id}.json 读（atoms 文件里的
已是融合值，不能当 LLM 原值用）。
用法: python3 -m packages.atomizer.blend_energy [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

from .enrich import ENERGY_BY_CATEGORY, energy_cost

ROOT = Path(__file__).resolve().parents[2]
DATA_ATOMS = ROOT / "data" / "atoms"
DATA_INTERIM = ROOT / "data" / "interim"
CACHE_DIR = ROOT / "data" / "cache" / "llm_enrich"


def norm_name(n: str | None) -> str:
    n = unicodedata.normalize("NFKD", n or "")
    n = "".join(ch for ch in n if not unicodedata.combining(ch))
    return re.sub(r"[\s\-‐‑‒–—―・･()（）\[\]【】'’]", "", n.lower())


def build_index(dest: str) -> dict[str, dict]:
    path = DATA_INTERIM / f"candidates_{dest}.json"
    idx: dict[str, dict] = {}
    if not path.exists():
        return idx
    for c in json.loads(path.read_text(encoding="utf-8"))["candidates"]:
        for nm in (c.get("name_local"), c.get("name_en")):
            k = norm_name(nm)
            if k and k not in idx:
                idx[k] = c
    return idx


def heuristic_energy(a: dict, idx: dict[str, dict]) -> int:
    for key in (a["name"].get("local"), a["name"].get("en")):
        c = idx.get(norm_name(key))
        if c:
            return energy_cost(c, a["categories"])
    return ENERGY_BY_CATEGORY.get(a["categories"][0], 3)


def run(dest: str, dry_run: bool) -> None:
    from .validate import write_atoms_atomic
    path = DATA_ATOMS / f"{dest}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    idx = build_index(dest)
    changed = 0
    skipped_no_cache = 0
    dist: Counter = Counter()
    for a in payload["atoms"]:
        # 幂等：LLM 原值只从缓存读；无缓存的原子跳过融合——
        # atoms 文件里的值可能已是融合结果，再融合会逐次漂移
        cache_p = CACHE_DIR / f"{a['id']}.json"
        if not cache_p.exists():
            skipped_no_cache += 1
            dist[a["constraints"]["energy_cost"]] += 1
            continue
        llm = json.loads(cache_p.read_text(encoding="utf-8")).get("energy_cost")
        if not isinstance(llm, int):
            skipped_no_cache += 1
            dist[a["constraints"]["energy_cost"]] += 1
            continue
        heur = heuristic_energy(a, idx)
        final = max(1, min(5, round(0.5 * llm + 0.5 * heur)))
        dist[final] += 1
        if final != a["constraints"]["energy_cost"]:
            changed += 1
            if not dry_run:
                a["constraints"]["energy_cost"] = final
    print(f"[blend] {dest}: {changed}/{len(payload['atoms'])} 调整"
          f"（跳过无缓存 {skipped_no_cache}）；分布 {dict(sorted(dist.items()))}")
    if not dry_run:
        write_atoms_atomic(dest, payload)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", action="append")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    for d in (args.dest or ["osaka", "bangkok", "abudhabi"]):
        run(d, args.dry_run)


if __name__ == "__main__":
    main()
