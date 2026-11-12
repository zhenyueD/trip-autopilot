"""demo CLI：自然语言 → IntentVector → 方案草稿。

用法：
    python3 -m packages.intent.cli "过年想带爸妈去个不累暖和的地方，大概5天"
    python3 -m packages.intent.cli "..." --atoms path/to/atoms.json
    python3 -m packages.intent.cli "..." --llm          # 走 LLM 解析（需 OPENAI_API_KEY / OPENAI_BASE_URL）

无 --llm 时走启发式解析（永远可用的降级路径，spec §6）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import matcher, parser_heuristic, schema
from .parser_llm import LLMUnavailableError, OpenAICompatibleBackend, sanitize_llm_vector

_WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DEFAULT_ATOMS = os.path.join(os.path.dirname(__file__), "data", "sample_atoms.json")
# 真实原子库（并行建设中，可能尚不存在）
_REAL_OSAKA_ATOMS = os.path.join(_WORKSPACE_ROOT, "data", "atoms", "osaka.json")


def _print_json(title: str, obj: object) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def parse_intent(text: str, use_llm: bool) -> dict:
    if not use_llm:
        return parser_heuristic.parse(text)
    # 混合解析（spec §5）：启发式结果为底，LLM 字段经 sanitize 逐字段覆盖
    base = parser_heuristic.parse(text)
    backend = OpenAICompatibleBackend()
    try:
        raw = backend.parse(text)
    except LLMUnavailableError as e:
        print(f"[llm] 不可用（{e}），回退启发式解析", file=sys.stderr)
        return base
    vec, dropped = sanitize_llm_vector(raw, text, base=base)
    print(f"[llm] 解析成功；被丢弃的非法字段：{dropped or '无'}", file=sys.stderr)
    return vec


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m packages.intent.cli",
        description="意图原子化 demo：自然语言 → IntentVector → 原子匹配方案草稿",
    )
    parser.add_argument("text", help="用户原始输入（中文自然语言）")
    parser.add_argument("--atoms", default=_DEFAULT_ATOMS,
                        help="原子库 JSON 路径（默认：内置 synthetic fixture）")
    parser.add_argument("--llm", action="store_true",
                        help="使用 LLM 解析（需 OPENAI_API_KEY / OPENAI_BASE_URL 环境变量；失败自动回退启发式）")
    args = parser.parse_args(argv)

    intent = parse_intent(args.text, args.llm)

    violations = schema.validate_intent_vector(intent)
    if violations:
        print(f"[error] IntentVector 校验失败：{violations}", file=sys.stderr)
        return 1
    _print_json("IntentVector", intent)

    atoms = matcher.load_atoms(args.atoms)
    plan = matcher.build_plan(intent, atoms)
    _print_json("方案草稿（Plan Draft）", plan)

    # ---- 集成冒烟测试：真实原子库存在则过一遍 matcher，不存在则跳过（不失败） ----
    if os.path.exists(_REAL_OSAKA_ATOMS):
        real_atoms = matcher.load_atoms(_REAL_OSAKA_ATOMS)
        real_plan = matcher.build_plan(intent, real_atoms)
        n_items = sum(len(d["items"]) for d in real_plan["days"])
        print(f"\n[smoke] 真实原子库 {_REAL_OSAKA_ATOMS}："
              f"{len(real_atoms)} 个原子 → 排入 {n_items} 项，排除 {len(real_plan['excluded_atoms'])} 项")
    else:
        print(f"\n[smoke] {_REAL_OSAKA_ATOMS} 尚不存在（atomizer 管线并行建设中），"
              "跳过真实原子库集成冒烟测试，本次运行于 synthetic fixture 模式")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
