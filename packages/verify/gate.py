"""校验门：check_atom(atom, when) —— 这个原子在这个时刻开不开。

数据来源优先级：
1. 原子自身 facts.opening_hours_raw（可选非破坏性字段，atomizer 未来可持久化；
   本包不写 data/atoms/*.json）
2. data/raw/overpass_{dest}_*.json 原始缓存，按 facts.source 中的
   osm:{type}/{id} 精确匹配（索引持久化到 data/cache/opening_hours_index_{dest}.json）
3. 都没有 → {"status": "unknown", "source": "none"}，绝不猜测。
"""
from __future__ import annotations

import glob
import json
import os
import sys
import tempfile
from datetime import datetime, date

from packages.verify import opening_hours as oh

RAW_DIR = "data/raw"
CACHE_DIR = "data/cache"

_INDEX_CACHE: dict[str, dict[str, str]] = {}  # dest → {osm_ref: opening_hours}


def _index_path(dest: str, cache_dir: str = CACHE_DIR) -> str:
    return os.path.join(cache_dir, f"opening_hours_index_{dest}.json")


def build_osm_index(dest: str, raw_dir: str = RAW_DIR,
                    cache_dir: str = CACHE_DIR, save: bool = True) -> dict[str, str]:
    """扫描 data/raw/overpass_{dest}_*.json，构建 osm:{type}/{id} → opening_hours 索引。"""
    index: dict[str, str] = {}
    for path in sorted(glob.glob(os.path.join(raw_dir, f"overpass_{dest}_*.json"))):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for e in data.get("elements", []):
            val = (e.get("tags") or {}).get("opening_hours")
            if val:
                index[f"osm:{e['type']}/{e['id']}"] = val
    if save:
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
        target = _index_path(dest, cache_dir)
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(target) + ".",
                                   suffix=".tmp", dir=cache_dir or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False, sort_keys=True)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    _INDEX_CACHE[dest] = index
    return index


def load_osm_index(dest: str, raw_dir: str = RAW_DIR,
                   cache_dir: str = CACHE_DIR) -> dict[str, str]:
    """加载索引：内存 → 磁盘缓存 → 现场重建。缓存损坏时告警并重建。"""
    if dest in _INDEX_CACHE:
        return _INDEX_CACHE[dest]
    path = _index_path(dest, cache_dir)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                _INDEX_CACHE[dest] = json.load(f)
            return _INDEX_CACHE[dest]
        except (OSError, ValueError) as e:
            print(f"[verify.gate] 索引缓存损坏，重建: {path} ({e})",
                  file=sys.stderr)
    return build_osm_index(dest, raw_dir, cache_dir)


def opening_hours_of(atom: dict, dest: str | None = None) -> tuple[str | None, str]:
    """取原子的 opening_hours 原文与来源。返回 (value|None, source)。"""
    facts = atom.get("facts", {})
    raw_field = facts.get("opening_hours_raw")
    if isinstance(raw_field, str) and raw_field.strip():
        return raw_field, "atom_facts"
    dest = dest or atom.get("destination", "")
    if not dest:
        return None, "none"
    index = load_osm_index(dest)
    for ref in facts.get("source", []):
        if ref.startswith("osm:") and ref in index:
            return index[ref], "osm_raw_cache"
    return None, "none"


def check_atom(atom: dict, when: datetime,
               holidays: set[date] | None = None) -> dict:
    """校验门：原子在 when 时刻的营业状态。

    返回 {"status": "open"|"closed"|"unknown", "source": str, "detail": str}。
    solver 集成约定见 packages/verify/__init__.py 文档串。
    """
    value, source = opening_hours_of(atom)
    if value is None:
        return {"status": "unknown", "source": "none",
                "detail": "no opening_hours data for this atom"}
    result = oh.evaluate(value, when, holidays=holidays)
    return {"status": result["status"], "source": source,
            "detail": f"opening_hours={value!r}: {result['detail']}"}
