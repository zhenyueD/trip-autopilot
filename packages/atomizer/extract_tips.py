"""extract_tips.py —— M1.1b 行家知识原子（tips）离线提取管道（schema v0.4）。

数据源纪律：仅用已有合规语料 —— enrich 阶段存入 candidates 的 Wikivoyage
listing 原文（wv_content）。LLM（DeepSeek）只做"抽取+翻译+结构化"，
禁止凭训练记忆生成；出处 URL 由代码从 facts.source 的 wikivoyage 条目
确定性推导，不让模型碰 URL。

用法:
  export DEEPSEEK_API_KEY=...
  python3 -m packages.atomizer.extract_tips --dest osaka --limit 10 --dry-run
  python3 -m packages.atomizer.extract_tips --dest osaka            # 跑 Top N（默认 100）缺缓存的
  python3 -m packages.atomizer.extract_tips --dest osaka --apply    # 合入 atoms（原子写回）
缓存: data/cache/tips/{atom_id}.json —— 幂等，复跑只补缺。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import time
from pathlib import Path

import requests

from .fetch_images import build_candidate_index, match_candidate, norm_name  # 复用候选索引
from .validate import APPLIES_TO_ALL

ROOT = Path(__file__).resolve().parents[2]
DATA_ATOMS = ROOT / "data" / "atoms"
CACHE_DIR = ROOT / "data" / "cache" / "tips"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = os.environ.get("ATOMIZER_LLM_MODEL", "deepseek-chat")
PROMPT_VERSION = "tips-v1"
BATCH_SIZE = 5
TOP_N = 100                 # 试点：按 wv_content 长度 Top 100
EXPIRES_DAYS = 90
MIN_CONFIDENCE = 0.5

SYSTEM = """你是旅行行家知识提取器。输入是若干 POI 的 Wikivoyage 英文原文（JSON 数组）。
从原文中抽取"经验性建议"（tacit knowledge）：避开人流的技巧、最佳/最差时段、入口与动线、
本地人怎么做、性价比提示、注意事项——就是导游朋友会悄悄告诉你的那种话。

严格纪律：
- 只抽取原文中实际存在的信息，可做翻译与浓缩，禁止添加原文没有的任何事实
- 原文只是客观描述、没有经验性内容时，返回空数组，不要硬编
- 每条 tip_zh ≤80 字，中文，口语化但准确
- applies_to 从枚举中选（可为空数组=通用）：persona: solo/couple/family/elderly/kids/group_friends/business；
  时段: early_morning/morning/noon/afternoon/sunset/evening/night/any；季节: spring/summer/autumn/winter
- confidence 0-1：原文支撑越直接越高

输出 JSON 数组，每项 {"id": "...", "tips": [{"tip_zh","applies_to","confidence"}]}，
每个 POI 最多 3 条。不要输出任何其他文字。"""


class FatalLLMError(RuntimeError):
    """4xx 等确定性错误，不重试。"""


def call_llm(batch_ctx: list[dict], api_key: str) -> list[dict]:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(batch_ctx, ensure_ascii=False)},
        ],
        "max_tokens": 3000,
        "temperature": 0.2,
    }
    last_err = None
    for attempt in range(3):
        try:
            r = requests.post(API_URL, json=payload, timeout=180,
                              headers={"Authorization": f"Bearer {api_key}"})
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:150]}")
            if r.status_code >= 400:
                raise FatalLLMError(f"HTTP {r.status_code}（不重试）: {r.text[:300]}")
            text = r.json()["choices"][0]["message"]["content"]
            return parse_json_array(text)
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


# ------------------------------------------------------------ 出处推导
def wv_source(atom: dict) -> dict | None:
    """facts.source 的 wikivoyage 条目 → {type, url}。URL 代码推导，模型不碰。"""
    for s in atom["facts"]["source"]:
        if not str(s).startswith("wikivoyage:"):
            continue
        ref = str(s)[len("wikivoyage:"):]
        page, _, anchor = ref.partition("#")
        url = "https://en.wikivoyage.org/wiki/" + page.replace(" ", "_")
        if anchor:
            url += "#" + anchor
        return {"type": "wikivoyage", "url": url}
    return None


def sanitize_tip(raw: dict, atom: dict, src: dict, today: datetime.date) -> dict | None:
    tz = (raw.get("tip_zh") or "").strip()
    if not tz or len(tz) > 80:
        return None
    applies = [t for t in (raw.get("applies_to") or []) if t in APPLIES_TO_ALL]
    try:
        conf = min(1.0, max(0.0, float(raw.get("confidence", 0.5))))
    except (TypeError, ValueError):
        conf = 0.5
    if conf < MIN_CONFIDENCE:
        return None
    return {
        "tip_zh": tz,
        "applies_to": applies,
        "source": src,
        "confidence": round(conf, 2),
        "extracted_at": today.isoformat(),
        "expires_at": (today + datetime.timedelta(days=EXPIRES_DAYS)).isoformat(),
    }


def load_cache(aid: str) -> dict | None:
    p = CACHE_DIR / f"{aid}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None


def run_dest(dest: str, dry_run: bool, limit: int, apply: bool) -> None:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit("缺少 DEEPSEEK_API_KEY 环境变量")
    payload = json.loads((DATA_ATOMS / f"{dest}.json").read_text(encoding="utf-8"))
    atoms = payload["atoms"]
    idx = build_candidate_index(dest)

    # 试点范围：有 wv_content + wikivoyage 出处的原子，按内容长度降序 Top N
    eligible: list[tuple[dict, str, dict]] = []   # (atom, wv_content, source)
    for a in atoms:
        src = wv_source(a)
        if not src:
            continue
        c = match_candidate(a, idx)
        content = (c or {}).get("wv_content") or ""
        if len(content.strip()) < 40:   # 太短没有信息量
            continue
        eligible.append((a, content.strip(), src))
    eligible.sort(key=lambda x: -len(x[1]))
    eligible = eligible[:TOP_N]
    todo = [e for e in eligible if not load_cache(e[0]["id"])]
    if limit:
        todo = todo[:limit]
    print(f"[tips] {dest}: 合格语料 {len(eligible)}（Top {TOP_N}），"
          f"已缓存 {len(eligible) - len(todo)}，本次 {len(todo)}", flush=True)

    today = datetime.date.today()
    n_tips = 0
    for i in range(0, len(todo), BATCH_SIZE):
        batch = todo[i:i + BATCH_SIZE]
        ctx = [{"id": a["id"],
                "name": {"en": a["name"].get("en"), "local": a["name"].get("local")},
                "categories": a["categories"],
                "wikivoyage_text": content[:1200]}
               for a, content, _src in batch]
        try:
            results = call_llm(ctx, api_key)
        except FatalLLMError:
            raise
        except RuntimeError as e:
            print(f"    [skip] {e}", flush=True)
            continue
        by_id = {r.get("id"): r for r in results if isinstance(r, dict)}
        for a, content, src in batch:
            raw = (by_id.get(a["id"]) or {}).get("tips") or []
            tips = [t for t in (sanitize_tip(x, a, src, today) for x in raw[:3]) if t]
            rec = {"tips": tips, "model": MODEL, "prompt_version": PROMPT_VERSION,
                   "extracted_at": today.isoformat()}
            if not dry_run:
                (CACHE_DIR / f"{a['id']}.json").write_text(
                    json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
            n_tips += len(tips)
        print(f"    批 {i // BATCH_SIZE + 1}: {len(batch)} 原子 → 累计 {n_tips} 条 tips", flush=True)

    if apply and not dry_run:
        from .validate import write_atoms_atomic
        n_atoms_with = 0
        for a in atoms:
            rec = load_cache(a["id"])
            if rec is not None:
                if rec.get("tips"):
                    a["tips"] = rec["tips"]
                    n_atoms_with += 1
            a["schema_version"] = "0.4"
        payload["schema_version"] = "0.4"
        write_atoms_atomic(dest, payload)
        total = sum(len(a.get("tips") or []) for a in atoms)
        print(f"[apply] {dest}: {n_atoms_with} 原子携带 {total} 条 tips，"
              f"schema v0.4 原子写回", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", action="append")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    for d in (args.dest or ["osaka"]):
        run_dest(d, args.dry_run, args.limit, args.apply)


if __name__ == "__main__":
    main()
