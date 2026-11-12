"""emit.py —— 产出最终原子库 data/atoms/{dest}.json 与 _registry.json（schema §5）。

id 规则：{destination}.{cluster}.{slug}，全库唯一。
用法：python3 -m packages.atomizer.emit [--dest ...]
"""
from __future__ import annotations

import argparse
import json
import sys

from .common import DATA_ATOMS, DATA_INTERIM, DESTINATIONS, slugify


def emit_dest(dest: str) -> dict:
    src = DATA_INTERIM / f"enriched_{dest}.json"
    blob = json.loads(src.read_text(encoding="utf-8"))
    used_ids: set[str] = set()
    atoms = []
    for a in blob["atoms"]:
        slug = slugify(a.pop("_slug_base", None) or
                       a.get("name", {}).get("en") or
                       a.get("name", {}).get("local") or "") or "spot"
        cluster = a["spatial"]["cluster"]
        base = f"{dest}.{cluster}.{slug}"
        aid, n = base, 2
        while aid in used_ids:
            aid = f"{base}_{n}"
            n += 1
        used_ids.add(aid)
        a.pop("_name_zh_real", None)
        a["id"] = aid
        # 字段顺序：id 在前
        ordered = {"id": aid}
        ordered.update({k: v for k, v in a.items() if k != "id"})
        atoms.append(ordered)

    payload = {
        "schema_version": "0.2",
        "destination": dest,
        "generated_by": "packages/atomizer (OSM Overpass + Wikivoyage)",
        "atom_count": len(atoms),
        "atoms": atoms,
    }
    path = DATA_ATOMS / f"{dest}.json"
    from .validate import write_atoms_atomic  # 延迟导入，避免与 validate 的 common 依赖成环
    write_atoms_atomic(dest, payload)
    print(f"[emit] {path} ({len(atoms)} atoms, 原子写回+内联校验通过)")
    return {"clusters": blob.get("clusters", [])}


def emit_registry(cluster_info: dict[str, dict]) -> None:
    """写 _registry.json。先读现有 registry 合并：--dest 单目的地运行时
    只更新本次 dest 的条目，其他目的地的 clusters 保持原值（修复旧实现
    把未运行目的地 clusters 清空重写的 bug）。"""
    import os
    import tempfile
    path = DATA_ATOMS / "_registry.json"
    existing: dict[str, dict] = {}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            existing = {d["id"]: d for d in old.get("destinations", [])}
        except json.JSONDecodeError:
            pass
    reg = {"schema_version": "0.2", "destinations": []}
    for dest, cfg in DESTINATIONS.items():
        if dest in cluster_info:
            clusters = [c["slug"] for c in cluster_info[dest].get("clusters", [])]
        else:
            clusters = existing.get(dest, {}).get("clusters", [])
        reg["destinations"].append({
            "id": dest,
            "name_zh": cfg["name_zh"],
            "center_lat": cfg["center"][0],
            "center_lon": cfg["center"][1],
            "currency": cfg["currency"],
            "timezone": cfg["timezone"],
            "clusters": clusters,
        })
    # 原子写：临时文件 + os.replace
    fd, tmp = tempfile.mkstemp(dir=str(DATA_ATOMS), prefix="._registry.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(reg, ensure_ascii=False, indent=1))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    print(f"[emit] {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", choices=list(DESTINATIONS), default=None)
    args = ap.parse_args()
    dests = [args.dest] if args.dest else list(DESTINATIONS)
    info = {}
    for d in dests:
        info[d] = emit_dest(d)
    emit_registry(info)


if __name__ == "__main__":
    sys.exit(main())
