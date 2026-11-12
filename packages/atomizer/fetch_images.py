"""fetch_images.py —— V1.1 P1：离线图片管道（schema v0.3 media 字段）。

优先级 1：候选有 wikidata QID → Wikidata P18 → Commons 原图（含授权元数据）
优先级 2：无 QID 但有 wikipedia 词条 → Wikipedia REST summary 页首图，
          再回 Commons imageinfo 补授权元数据（拿不到授权就放弃该图，保真实性）
优先级 3：都没有 → media 全 null（占位图由呈现层负责）。禁止编造、禁止盗链。

用法:
  python3 -m packages.atomizer.fetch_images --dest osaka [--dry-run] [--limit N]
缓存: data/cache/images/{atom_id}.json —— 幂等，复跑只补缺；--apply 才合入 atoms。
"""
from __future__ import annotations

import argparse
import datetime
import html
import json
import re
import time
import unicodedata
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
DATA_ATOMS = ROOT / "data" / "atoms"
DATA_INTERIM = ROOT / "data" / "interim"
CACHE_DIR = ROOT / "data" / "cache" / "images"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": "atom-travel-atomizer/1.1 (offline image pipeline; wikimedia/wikipedia open-licensed images only)"}
RATE_LIMIT_S = 0.5
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WD_BATCH = 50

_last_call = 0.0


def _get(url: str, params: dict | None = None) -> dict:
    """限速 GET（≥0.5s 间隔），指数退避 2 次。"""
    global _last_call
    wait = RATE_LIMIT_S - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    last_err = None
    for attempt in range(3):
        try:
            _last_call = time.monotonic()
            r = requests.get(url, params=params, headers=UA, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(6 * (attempt + 1))   # 6/12/18s：Wikimedia 429 需要更长退避
    raise RuntimeError(f"GET {url} 失败（3 次）: {last_err}")


def norm_name(n: str | None) -> str:
    n = unicodedata.normalize("NFKD", n or "")
    n = "".join(ch for ch in n if not unicodedata.combining(ch))
    return re.sub(r"[\s\-‐‑‒–—―・･()（）\[\]【】'’]", "", n.lower())


def strip_html(s: str | None) -> str | None:
    if not s:
        return None
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip() or None


# ------------------------------------------------------------ 候选索引
def build_candidate_index(dest: str) -> dict[str, list[dict]]:
    path = DATA_INTERIM / f"candidates_{dest}.json"
    idx: dict[str, list[dict]] = {}
    if not path.exists():
        return idx
    for c in json.loads(path.read_text(encoding="utf-8"))["candidates"]:
        for nm in (c.get("name_local"), c.get("name_en")):
            k = norm_name(nm)
            if k:
                idx.setdefault(k, []).append(c)
    return idx


def match_candidate(a: dict, idx: dict[str, list[dict]]) -> dict | None:
    """按规范化名匹配候选；同名多个时取坐标最近者。"""
    for key in (a["name"].get("local"), a["name"].get("en")):
        hits = idx.get(norm_name(key))
        if hits:
            if len(hits) == 1:
                return hits[0]
            la, lo = a["spatial"]["lat"], a["spatial"]["lon"]
            return min(hits, key=lambda c: (c["lat"] - la) ** 2 + (c["lon"] - lo) ** 2)
    return None


# ------------------------------------------------------------ Wikidata / Commons
def wd_p18_batch(qids: list[str]) -> dict[str, str]:
    """QID → P18 Commons 文件名（File: 前缀去掉）。批量 50/请求。"""
    out: dict[str, str] = {}
    for i in range(0, len(qids), WD_BATCH):
        batch = qids[i:i + WD_BATCH]
        try:
            data = _get(WIKIDATA_API, {"action": "wbgetentities", "ids": "|".join(batch),
                                       "props": "claims", "format": "json"})
        except RuntimeError as e:
            print(f"  [wd] P18 批 {i // WD_BATCH + 1} 失败（跳过）: {e}", flush=True)
            continue
        for qid, ent in (data.get("entities") or {}).items():
            claims = (ent.get("claims") or {}).get("P18") or []
            for cl in claims:
                v = ((cl.get("mainsnak") or {}).get("datavalue") or {}).get("value")
                if v:
                    out[qid] = v
                    break
        print(f"  [wd] P18 批 {i // WD_BATCH + 1}: {len(batch)} QID → 累计 {len(out)} 有图", flush=True)
    return out


def _norm_file_title(t: str) -> str:
    """MediaWiki 标题规范化对齐：空格→下划线（查询侧用下划线文件名做 key）。"""
    return t.replace(" ", "_")


def commons_imageinfo_batch(titles: list[str]) -> dict[str, dict]:
    """File 标题 → {url, license, author}。批量 50/请求；拿不到授权的剔除。"""
    out: dict[str, dict] = {}
    for i in range(0, len(titles), WD_BATCH):
        batch = titles[i:i + WD_BATCH]
        try:
            data = _get(COMMONS_API, {
                "action": "query", "titles": "|".join(batch), "prop": "imageinfo",
                "iiprop": "url|extmetadata", "iiurlwidth": "800", "format": "json"})
        except RuntimeError as e:
            print(f"  [commons] 批 {i // WD_BATCH + 1} 失败（跳过，可用 --retry-empty 补）: {e}",
                  flush=True)
            continue
        for page in (data.get("query", {}).get("pages") or {}).values():
            title = page.get("title")
            ii = (page.get("imageinfo") or [None])[0]
            if not title or not ii:
                continue
            em = ii.get("extmetadata") or {}
            license_short = strip_html((em.get("LicenseShortName") or {}).get("value"))
            url = ii.get("thumburl") or ii.get("url")
            if not url or not license_short:
                continue  # 无授权信息 → 不用（纪律）
            out[_norm_file_title(title)] = {"url": url, "license": license_short,
                                            "author": strip_html((em.get("Artist") or {}).get("value"))}
        print(f"  [commons] 批 {i // WD_BATCH + 1}: {len(batch)} 文件 → 累计 {len(out)} 可用", flush=True)
    return out


def wikipedia_pageimage(wikipedia_tag: str) -> tuple[str, str] | None:
    """wikipedia tag（如 "en:Osaka Castle"）→ (文件名, lang)。无 originalimage 返回 None。"""
    m = re.match(r"^([a-z-]+):(.+)$", wikipedia_tag, re.I)
    if not m:
        return None
    lang, title = m.group(1), m.group(2)
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title.replace(' ', '_')}"
    try:
        data = _get(url)
    except RuntimeError:
        return None
    img = (data.get("originalimage") or {}).get("source")
    if not img:
        return None
    # URL 形如 .../wikipedia/commons/x/xx/Filename.jpg?utm_source=... → 先剥 query 再取文件名
    fname = img.split("?", 1)[0].rsplit("/", 1)[-1]
    try:
        from urllib.parse import unquote
        fname = unquote(fname)
    except Exception:  # noqa: BLE001
        pass
    return (fname, lang) if fname else None


# 本地 wiki 图（ja/th 等站自托管）授权白名单：只收明确自由授权，
# 拒绝日本/泰国站常见的合理使用（フェアユース / fair use / non-free）
FREE_LICENSE = re.compile(
    r"creative\s*commons|\bcc[\s-]|cc0|public\s*domain|gfdl|\bpd\b|パブリックドメイン|著作権.*?(放棄|消滅)",
    re.I)
NONFREE_LICENSE = re.compile(r"fair\s*use|フェアユース|non.?free|合理使用", re.I)


def localwiki_imageinfo_batch(lang: str, titles: list[str]) -> dict[str, dict]:
    """源语言维基本地文件的 imageinfo（ja.wikipedia.org 等自托管图）。
    只收自由授权；无授权信息/合理使用一律剔除。批量 50/请求。"""
    out: dict[str, dict] = {}
    api = f"https://{lang}.wikipedia.org/w/api.php"
    for i in range(0, len(titles), WD_BATCH):
        batch = titles[i:i + WD_BATCH]
        try:
            data = _get(api, {"action": "query", "titles": "|".join(batch),
                              "prop": "imageinfo", "iiprop": "url|extmetadata",
                              "iiurlwidth": "800", "format": "json"})
        except RuntimeError:
            continue
        for page in (data.get("query", {}).get("pages") or {}).values():
            title = page.get("title")
            ii = (page.get("imageinfo") or [None])[0]
            if not title or not ii:
                continue
            em = ii.get("extmetadata") or {}
            lic = strip_html((em.get("LicenseShortName") or {}).get("value"))
            url = ii.get("thumburl") or ii.get("url")
            if not url or not lic:
                continue
            if NONFREE_LICENSE.search(lic) or not FREE_LICENSE.search(lic):
                continue  # 授权不明确/合理使用 → 不用（纪律）
            out[_norm_file_title(title)] = {"url": url, "license": lic,
                                            "author": strip_html((em.get("Artist") or {}).get("value"))}
    print(f"  [{lang}-local] {len(titles)} 文件 → {len(out)} 自由授权可用", flush=True)
    return out


# ------------------------------------------------------------ 主流程
def empty_media() -> dict:
    return {"image_url": None, "image_source": None,
            "image_license": None, "image_author": None}


def run_dest(dest: str, dry_run: bool, limit: int, apply: bool, retry_empty: bool) -> dict:
    payload = json.loads((DATA_ATOMS / f"{dest}.json").read_text(encoding="utf-8"))
    atoms = payload["atoms"]
    idx = build_candidate_index(dest)

    # 已缓存的直接读；--retry-empty 把缓存里的无图条目当未完成重试
    todo: list[tuple[dict, dict | None]] = []
    cached_n = 0
    for a in atoms:
        p = CACHE_DIR / f"{a['id']}.json"
        if p.exists():
            if retry_empty:
                m = json.loads(p.read_text(encoding="utf-8"))["media"]
                if not m.get("image_url"):
                    todo.append((a, match_candidate(a, idx)))
                    continue
            cached_n += 1
            continue
        todo.append((a, match_candidate(a, idx)))
    if limit:
        todo = todo[:limit]
    print(f"[images] {dest}: 总数 {len(atoms)}，已缓存 {cached_n}，本次处理 {len(todo)}", flush=True)

    # Phase 1: 收集 wikidata QID，批量查 P18
    qid_map: dict[str, str] = {}   # atom_id → qid
    for a, c in todo:
        if c and c.get("wikidata"):
            qid_map[a["id"]] = c["wikidata"]
    p18 = wd_p18_batch(sorted(set(qid_map.values()))) if qid_map else {}
    # Phase 2: P18 文件名批量查 Commons 授权
    file_titles = ["File:" + f for f in set(p18.values())]
    commons = commons_imageinfo_batch(file_titles) if file_titles else {}

    results: dict[str, dict] = {}
    stats = {"wikimedia_commons": 0, "wikipedia": 0, "none": 0, "no_candidate": 0}
    today = datetime.date.today().isoformat()

    def _store(aid: str, media: dict) -> None:
        """每条结果立即落缓存——中断后复跑只补缺（长尾部是 Wikipedia 逐个 REST）。"""
        results[aid] = media
        if not dry_run:
            (CACHE_DIR / f"{aid}.json").write_text(json.dumps(
                {"media": media, "fetched_at": today, "via": "fetch_images v1"},
                ensure_ascii=False, indent=1), encoding="utf-8")

    # Phase 3: 逐原子定案；P18 未中的走 Wikipedia REST（逐个，限速）
    wp_queue: list[tuple[str, str]] = []   # (atom_id, wikipedia tag)
    for a, c in todo:
        aid = a["id"]
        if not c:
            _store(aid, empty_media())
            stats["no_candidate"] += 1
            continue
        qid = qid_map.get(aid)
        got = False
        if qid and qid in p18:
            info = commons.get(_norm_file_title("File:" + p18[qid]))
            if info:
                _store(aid, {"image_url": info["url"], "image_source": "wikimedia_commons",
                             "image_license": info["license"], "image_author": info["author"]})
                stats["wikimedia_commons"] += 1
                got = True
        if not got and c.get("wikipedia"):
            wp_queue.append((aid, c["wikipedia"]))
        elif not got:
            _store(aid, empty_media())
            stats["none"] += 1

    # Phase 4: Wikipedia 页首图 → 文件名 → 批量 Commons 授权
    wp_files: dict[str, str] = {}
    wp_lang: dict[str, str] = {}
    for j, (aid, tag) in enumerate(wp_queue):
        r = wikipedia_pageimage(tag)
        if r:
            wp_files[aid], wp_lang[aid] = r
        else:
            _store(aid, empty_media())   # REST 无图：立即定案
            stats["none"] += 1
        if (j + 1) % 25 == 0:
            print(f"  [wp] {j + 1}/{len(wp_queue)} 页首图探查", flush=True)
    wp_commons = commons_imageinfo_batch(
        ["File:" + f for f in set(wp_files.values())]) if wp_files else {}
    # Phase 5: Commons 未中的，回源语言维基本地图（ja/th 站自托管，仅收自由授权）
    local_miss: dict[str, list[str]] = {}
    for aid, fname in wp_files.items():
        if _norm_file_title("File:" + fname) not in wp_commons:
            local_miss.setdefault(wp_lang[aid], []).append("File:" + fname)
    local_info: dict[str, dict] = {}
    for lang, titles in local_miss.items():
        local_info.update(localwiki_imageinfo_batch(lang, sorted(set(titles))))
    for aid, tag in wp_queue:
        if aid in results:
            continue  # 已在上面定案
        fname = wp_files.get(aid)
        key = _norm_file_title("File:" + fname) if fname else None
        info = (wp_commons.get(key) if key else None) \
            or (local_info.get(key) if key else None)
        if info:
            _store(aid, {"image_url": info["url"], "image_source": "wikipedia",
                         "image_license": info["license"], "image_author": info["author"]})
            stats["wikipedia"] += 1
        else:
            _store(aid, empty_media())
            stats["none"] += 1

    # 合入 atoms（含历史缓存）
    if apply and not dry_run:
        from .validate import write_atoms_atomic
        have_img = 0
        for a in atoms:
            p = CACHE_DIR / f"{a['id']}.json"
            media = json.loads(p.read_text(encoding="utf-8"))["media"] if p.exists() \
                else results.get(a["id"], empty_media())
            a["media"] = media
            a["schema_version"] = "0.3"
            if media.get("image_url"):
                have_img += 1
        payload["schema_version"] = "0.3"
        write_atoms_atomic(dest, payload)
        print(f"[apply] {dest}: 有图 {have_img}/{len(atoms)} "
              f"({100.0 * have_img / len(atoms):.1f}%)，schema v0.3 原子写回", flush=True)

    print(f"[images] {dest}: 本次来源分布 {stats}", flush=True)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", action="append")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--apply", action="store_true", help="把缓存合入 atoms 文件（原子写回）")
    ap.add_argument("--retry-empty", action="store_true",
                    help="把缓存中无图的条目当未完成重试（走本地 wiki 兜底等新增路径）")
    args = ap.parse_args()
    for d in (args.dest or ["osaka", "bangkok", "abudhabi"]):
        run_dest(d, args.dry_run, args.limit, args.apply, args.retry_empty)


if __name__ == "__main__":
    main()
