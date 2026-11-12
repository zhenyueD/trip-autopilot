"""BACKLOG #4：LLM 精化管线（DeepSeek）。

只动表达层，绝不碰事实层：
  可改: name.zh / summary / emotion_tags / constraints.energy_cost / season_fit(仅气候敏感)
  禁改: 坐标 / facts.source / cluster / 价格 / categories（类别建议写入 review queue）

用法:
  export DEEPSEEK_API_KEY=...
  python3 -m packages.atomizer.llm_enrich --pilot 20          # 试跑并打印 before/after
  python3 -m packages.atomizer.llm_enrich --apply             # 全量精化并覆写 atoms
缓存: data/cache/llm_enrich/{atom_id}.json —— 每批落盘，中断可续跑。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import time
import unicodedata
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
DATA_ATOMS = ROOT / "data" / "atoms"
DATA_INTERIM = ROOT / "data" / "interim"
CACHE_DIR = ROOT / "data" / "cache" / "llm_enrich"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

API_URL = "https://api.deepseek.com/chat/completions"
# 默认 deepseek-chat（DeepSeek 官方稳定别名；本网关将其映射到 deepseek-flash，
# 实测 2026-09-12 可用）。曾被审查指出默认 deepseek-v4-pro 为推理模型、
# 单批 15 原子超 290s 不可用。可用 ATOMIZER_LLM_MODEL 覆盖。
MODEL = os.environ.get("ATOMIZER_LLM_MODEL", "deepseek-chat")
PROMPT_VERSION = "v1"
BATCH_SIZE = 15
MAX_TOKENS = 6000
SUMMARY_MAX = 60  # 与 docs/SUPPLY-ATOM-SCHEMA.md 合同一致（≤60字）

EMOTIONS_ALL = {
    "healing", "lively_local", "photo", "foodie", "romantic", "family_fun",
    "nostalgic", "luxury", "adventure", "quiet_escape", "cultural_deep",
    "night_view", "bargain", "iconic", "hidden_gem",
}
SEASONS = ("spring", "summer", "autumn", "winter")
CLIMATE_SENSITIVE = {"nature_park", "market_street", "transport_experience",
                     "day_trip", "photo_spot", "viewpoint"}

TAG_KEYS = ("tourism", "historic", "amenity", "leisure", "shop", "cuisine",
            "attraction", "route", "wikidata", "wikipedia", "website", "description")


def norm_name(n: str | None) -> str:
    n = unicodedata.normalize("NFKD", n or "")
    n = "".join(ch for ch in n if not unicodedata.combining(ch))
    return re.sub(r"[\s\-‐‑‒–—―・･()（）\[\]【】'’]", "", n.lower())


# ------------------------------------------------------------ 候选上下文索引
def build_candidate_index(dest: str) -> dict[str, dict]:
    """norm_name → candidate（含 OSM tags / wikivoyage 摘要），供 prompt 上下文。"""
    path = DATA_INTERIM / f"candidates_{dest}.json"
    if not path.exists():
        return {}
    cands = json.loads(path.read_text(encoding="utf-8"))["candidates"]
    idx: dict[str, dict] = {}
    for c in cands:
        for nm in (c.get("name_local"), c.get("name_en")):
            k = norm_name(nm)
            if k and k not in idx:
                idx[k] = c
    return idx


def atom_context(a: dict, idx: dict[str, dict]) -> dict:
    ctx = {
        "id": a["id"],
        "name_local": a["name"].get("local"),
        "name_en": a["name"].get("en"),
        "name_zh_current": a["name"].get("zh"),
        "categories": a.get("categories"),
        "cluster": a["spatial"].get("cluster"),
        "lat": a["spatial"].get("lat"), "lon": a["spatial"].get("lon"),
        "summary_current": a.get("summary"),
        "emotion_tags_current": a.get("emotion_tags"),
        "energy_cost_current": (a.get("constraints") or {}).get("energy_cost"),
        "season_fit_current": (a.get("time_structure") or {}).get("season_fit"),
        "weather_dependency": (a.get("time_structure") or {}).get("weather_dependency"),
    }
    for key in (a["name"].get("local"), a["name"].get("en")):
        c = idx.get(norm_name(key))
        if c:
            tags = {k: v for k, v in (c.get("tags") or {}).items() if k in TAG_KEYS}
            ctx["osm_tags"] = tags or None
            if c.get("wv_content"):
                ctx["wikivoyage"] = c["wv_content"][:200]
            break
    return {k: v for k, v in ctx.items() if v is not None}


# ------------------------------------------------------------ Prompt
SYSTEM = """你是旅行供给数据精化器。输入是一批旅行 POI 原子（JSON 数组），你只精化"表达层"，绝不编造事实。

对每个原子输出：
- name_zh: 通行中文名（如 "Osaka Castle"→"大阪城"）。不确定/无通行译名时输出 null，禁止机械音译。
- summary: 中文一句话 ≤60 字，有情绪有画面（如"夕阳把海湾染成金色时，摩天轮转到最高点"）。禁止编造价格、营业时间、排名等事实；信息不足就基于类别+气候写氛围。
- emotion_tags: 从枚举选 2-5 个：healing, lively_local, photo, foodie, romantic, family_fun, nostalgic, luxury, adventure, quiet_escape, cultural_deep, night_view, bargain, iconic, hidden_gem
- energy_cost: 1-5 整数（1=坐着喝咖啡，3=正常步行游览，5=主题乐园/全日徒步）
- season_fit: 仅当该场所明显受季节/气候影响（户外、自然景观、市集、游船）才输出，四键 spring/summer/autumn/winter，各 0.0-1.0；否则输出 null
- confidence: 0.0-1.0，你对 name_zh 和 summary 的总体把握
- needs_review: true 当 confidence<0.7 或你感到不确定

严格输出 JSON 数组，每项 {"id","name_zh","summary","emotion_tags","energy_cost","season_fit","confidence","needs_review"}，不要输出任何其他文字。"""


class FatalLLMError(RuntimeError):
    """4xx 等确定性错误，不重试。"""


def call_llm(batch_ctx: list[dict], api_key: str) -> tuple[list[dict], dict]:
    """调用 DeepSeek，返回 (结果数组, usage)。指数退避重试 3 次。"""
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(batch_ctx, ensure_ascii=False)},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.3,
    }
    last_err = None
    for attempt in range(3):
        try:
            r = requests.post(API_URL, json=payload, timeout=240,
                              headers={"Authorization": f"Bearer {api_key}"})
            # 4xx（除 429 限流）是请求侧确定性错误（模型名错/欠费/参数非法），
            # 重试无意义：立即失败且带上响应体，不浪费 35s 退避
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code >= 400:
                raise FatalLLMError(f"HTTP {r.status_code}（不重试）: {r.text[:300]}")
            data = r.json()
            text = data["choices"][0]["message"]["content"]
            return parse_json_array(text), data.get("usage") or {}
        except FatalLLMError:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = 5 * (2 ** attempt)
            print(f"    [retry] 第{attempt + 1}次失败: {e}; {wait}s 后重试", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"批次失败（3 次重试后）: {last_err}")


def parse_json_array(text: str) -> list[dict]:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\[.*\])\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        s, e = text.find("["), text.rfind("]")
        if s >= 0 and e > s:
            text = text[s:e + 1]
    return json.loads(text)


# ------------------------------------------------------------ 消毒
def sanitize(item: dict, atom: dict) -> dict | None:
    """校验+清洗单条 LLM 输出；不合法字段回落到原启发式值。返回可缓存的结果。"""
    if item.get("id") != atom["id"]:
        return None
    out: dict = {"id": atom["id"]}
    zh = item.get("name_zh")
    if isinstance(zh, str) and zh.strip():
        out["name_zh"] = zh.strip()
    s = item.get("summary")
    if isinstance(s, str) and s.strip():
        out["summary"] = s.strip()[:SUMMARY_MAX]
    tags = [t for t in (item.get("emotion_tags") or []) if t in EMOTIONS_ALL]
    if 2 <= len(tags) <= 5:
        out["emotion_tags"] = list(dict.fromkeys(tags))
    ec = item.get("energy_cost")
    if isinstance(ec, (int, float)) and 1 <= round(ec) <= 5:
        out["energy_cost"] = int(round(ec))
    sf = item.get("season_fit")
    if isinstance(sf, dict) and set(sf) >= set(SEASONS):
        try:
            out["season_fit"] = {k: min(1.0, max(0.0, float(sf[k]))) for k in SEASONS}
        except (TypeError, ValueError):
            pass
    try:
        out["confidence"] = min(1.0, max(0.0, float(item.get("confidence", 0.5))))
    except (TypeError, ValueError):
        out["confidence"] = 0.5
    out["needs_review"] = bool(item.get("needs_review")) or out["confidence"] < 0.7
    return out


# ------------------------------------------------------------ 应用
def apply_result(a: dict, res: dict) -> None:
    if res.get("name_zh"):
        a["name"]["zh"] = res["name_zh"]
    if res.get("summary"):
        a["summary"] = res["summary"]
    if res.get("emotion_tags"):
        a["emotion_tags"] = res["emotion_tags"]
    if res.get("energy_cost"):
        a.setdefault("constraints", {})["energy_cost"] = res["energy_cost"]
    if res.get("season_fit") and a["categories"][0] in CLIMATE_SENSITIVE:
        a.setdefault("time_structure", {})["season_fit"] = res["season_fit"]
    a["llm_enrichment"] = {
        "model": MODEL,
        "model_date": datetime.date.today().isoformat(),
        "prompt_version": PROMPT_VERSION,
        "confidence": res["confidence"],
        "needs_review": res["needs_review"],
    }
    # needs_review=True 的原子不能标记精化完成，否则 validate 的 llm_pending
    # 统计失真（人工复核队列与"待精化"口径必须一致）
    a["facts"]["needs_llm_enrichment"] = bool(res["needs_review"])


def load_cache(atom_id: str) -> dict | None:
    p = CACHE_DIR / f"{atom_id}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None


def save_cache(res: dict) -> None:
    (CACHE_DIR / f"{res['id']}.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", action="append")
    ap.add_argument("--pilot", type=int, default=0, help="只处理前 N 个未精化原子，打印样本，不写 atoms")
    ap.add_argument("--apply", action="store_true", help="把缓存结果写回 atoms 文件")
    args = ap.parse_args()
    # 禁止 --pilot 与 --apply 同用：apply 会写回全部历史缓存，
    # 与"只试跑 N 个"的语义冲突，容易把未评审的批量结果误写进 atoms
    if args.pilot and args.apply:
        ap.error("--pilot 与 --apply 禁止同用（pilot 仅试跑打印样本；"
                 "确认质量后去掉 --pilot 单独跑 --apply）")

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit("缺少 DEEPSEEK_API_KEY 环境变量")

    dests = args.dest or ["osaka", "bangkok", "abudhabi"]
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    stats = {"cached": 0, "new": 0, "failed": 0}

    for dest in dests:
        payload = json.loads((DATA_ATOMS / f"{dest}.json").read_text(encoding="utf-8"))
        atoms = payload["atoms"]
        idx = build_candidate_index(dest)
        todo = [a for a in atoms if not load_cache(a["id"])]
        stats["cached"] += len(atoms) - len(todo)
        if args.pilot:
            todo = todo[:args.pilot]
        print(f"[llm] {dest}: 待精化 {len(todo)}（已有缓存 {len(atoms) - len(todo)}）", flush=True)

        for i in range(0, len(todo), BATCH_SIZE):
            batch = todo[i:i + BATCH_SIZE]
            ctx = [atom_context(a, idx) for a in batch]
            try:
                results, usage = call_llm(ctx, api_key)
            except FatalLLMError:
                raise  # 4xx 确定性错误：整轮终止，不继续烧后续批次
            except RuntimeError as e:
                print(f"    [skip] {e}", flush=True)
                stats["failed"] += len(batch)
                continue
            for k in usage_total:
                usage_total[k] += int(usage.get(k, 0))
            by_id = {r.get("id"): r for r in results if isinstance(r, dict)}
            for a in batch:
                res = sanitize(by_id.get(a["id"], {}), a)
                if res:
                    save_cache(res)      # 每批立即落盘
                    stats["new"] += 1
                else:
                    stats["failed"] += 1
            print(f"    批 {i // BATCH_SIZE + 1}: {len(by_id)}/{len(batch)} 条已缓存", flush=True)

        # pilot：打印 before/after 样本
        if args.pilot:
            shown = 0
            for a in atoms:
                res = load_cache(a["id"])
                if not res or shown >= 5:
                    continue
                print(f"\n--- {a['id']} ---")
                print(f"  zh : {a['name'].get('zh')} → {res.get('name_zh')}")
                print(f"  sum: {(a.get('summary') or '')[:50]} → {(res.get('summary') or '')[:60]}")
                print(f"  emo: {a.get('emotion_tags')} → {res.get('emotion_tags')}")
                print(f"  nrg: {(a.get('constraints') or {}).get('energy_cost')} → {res.get('energy_cost')}"
                      f"  conf={res.get('confidence')} review={res.get('needs_review')}")
                shown += 1

        # apply：原子性写回（临时文件 + os.replace + 落盘前内联校验）
        if args.apply:
            from .validate import write_atoms_atomic
            applied = 0
            for a in atoms:
                res = load_cache(a["id"])
                if res:
                    apply_result(a, res)
                    applied += 1
            payload["atom_count"] = len(atoms)
            write_atoms_atomic(dest, payload)
            print(f"[apply] {dest}: {applied}/{len(atoms)} 原子已精化（原子写回+内联校验通过）", flush=True)

    print(f"[usage] prompt={usage_total['prompt_tokens']} completion={usage_total['completion_tokens']} "
          f"| cached={stats['cached']} new={stats['new']} failed={stats['failed']}", flush=True)
    (CACHE_DIR / "_usage.json").write_text(json.dumps(
        {"model": MODEL, "date": datetime.date.today().isoformat(),
         **usage_total, **stats}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
