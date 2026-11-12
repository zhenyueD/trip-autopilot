"""显式点名 POI 锚定（BACKLOG #1）：意图文本 → atom_id 列表。

检测层：原子名（zh/en/local）归一化包含匹配 + 手工别名表（著名地标的口语叫法）。
归一化：小写、去空白/标点。中文名 ≥2 字、英文名 ≥4 字符才参与包含匹配（防误命中）。
确定性：按 (匹配串长度降序, atom_id 字典序) 排序，去重，封顶 5 个。

集成路径：parser_heuristic.parse() 产出 IntentVector（纯文本理解，不碰原子库）；
本模块的 enrich_intent_with_pois() 在服务端拿到原子库后回填 vec["explicit_pois"]。
solver 把这些 atom 钉进其 cluster 所在日，收据标注"你点名要去"。
"""
from __future__ import annotations

import re

# 著名地标口语别名 → 原子 id 片段（全库唯一后缀匹配）。确定性事实，人工维护。
POI_ALIASES: dict[str, list[str]] = {
    "abudhabi": {
        "大清真寺": "grand_mosque",
        "谢赫扎耶德": "grand_mosque",
        "grand mosque": "grand_mosque",
        "卢浮宫": "louvre",
    },
    "osaka": {
        "环球影城": "universal_studios_japan",
        "usj": "universal_studios_japan",
        "大阪城": "osaka_castle",
        "道顿堀": "dotonbori",
        "通天阁": "tsutenkaku",
    },
    "bangkok": {
        "大皇宫": "grand_palace",
        "郑王庙": "wat_arun",
        "卧佛寺": "wat_pho",
    },
}

_NORM_RE = re.compile(r"[\s\-–—_·,，。.!！?？'\"'()（）]+")
MAX_EXPLICIT_POIS = 5


def _norm(s: str | None) -> str:
    if not s:
        return ""
    return _NORM_RE.sub("", s.lower())


def detect_explicit_pois(text: str, atoms: list[dict], dest: str) -> list[str]:
    """意图文本 → 点名的 atom_id 列表（确定性）。"""
    norm_text = _norm(text)
    hits: list[tuple[int, str]] = []  # (匹配串长度, atom_id)

    aliases = POI_ALIASES.get(dest, {})
    # 别名子串碰撞保护（审查 #16）：同一 id_frag 可能命中多个 atom
    # （如 osaka_castle 同时是 osaka_castle_garden 的子串）——每个别名
    # 只锚定"最精确"的一个：id 以 frag 结尾者优先，仍多个取 id 最短者。
    alias_target: dict[str, str] = {}
    for alias, id_frag in aliases.items():
        if _norm(alias) not in norm_text:
            continue
        pool = sorted(a.get("id", "") for a in atoms
                      if a.get("destination") == dest and id_frag in a.get("id", ""))
        if pool:
            exact = [i for i in pool if i.endswith(id_frag)]
            alias_target[alias] = min(exact or pool, key=len)

    for a in atoms:
        aid = a.get("id", "")
        if a.get("destination") != dest:
            continue
        names = a.get("name", {})
        matched_len = 0
        for variant in (names.get("zh"), names.get("en"), names.get("local")):
            nv = _norm(variant)
            # 中文 ≥2 字 / 其他 ≥4 字符才做包含匹配
            if nv and len(nv) >= (2 if re.search(r"[一-鿿]", nv) else 4) and nv in norm_text:
                matched_len = max(matched_len, len(nv))
        for alias in aliases:
            if aid == alias_target.get(alias):
                matched_len = max(matched_len, len(_norm(alias)) + 100)  # 别名命中优先
        if matched_len:
            hits.append((matched_len, aid))

    # 确定性排序：匹配长度降序 → atom_id 字典序；同一原子去重
    hits.sort(key=lambda x: (-x[0], x[1]))
    seen: set[str] = set()
    out: list[str] = []
    for _, aid in hits:
        if aid not in seen:
            seen.add(aid)
            out.append(aid)
        if len(out) >= MAX_EXPLICIT_POIS:
            break
    return out


def enrich_intent_with_pois(vec: dict, atoms: list[dict], dest: str) -> dict:
    """parse 之后、求解之前调用：把点名 POI 回填进 IntentVector.explicit_pois。"""
    found = detect_explicit_pois(vec.get("raw_input", ""), atoms, dest)
    vec["explicit_pois"] = found
    return vec
