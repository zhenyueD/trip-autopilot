"""审计报告：python3 -m packages.verify.audit --dest osaka [--when ISO] [--sample 10]

输出（打印 + 可选 --out 落盘 JSON）：
1. 原子 opening_hours 数据覆盖率（n/%）
2. 解析器对这些真实值的解析覆盖率
3. 指定时刻的 N 个抽样校验
4. 样本方案校验：只读调用 packages.solver.solve_refine 生成一份样本方案，
   列出「在其计划槽位时刻会闭店」的原子（solver 不可导入/失败时跳过并注明）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, date, timedelta

from packages.verify import gate, opening_hours as oh

ATOMS_DIR = "data/atoms"


def _load_registry() -> dict:
    with open(os.path.join(ATOMS_DIR, "_registry.json"), encoding="utf-8") as f:
        return json.load(f)


def _load_atoms(dest: str) -> list[dict]:
    with open(os.path.join(ATOMS_DIR, f"{dest}.json"), encoding="utf-8") as f:
        return json.load(f)["atoms"]


def _sample_plan_checks(dest: str, atoms: list[dict], registry_entry: dict,
                        when: datetime) -> dict:
    """只读使用 solver 生成样本方案，校验每个 atom 槽位。失败则跳过。"""
    out = {"available": False, "closed_items": [], "checked": 0, "note": None}
    try:
        from packages.solver import refine  # 只读使用，不修改 solver
    except Exception as e:  # pragma: no cover
        out["note"] = f"solver not importable: {e}"
        return out
    intent = {
        "time": {"duration_days": 3},
        "pace": {"atoms_per_day": 3},
        "party": {"type": "couple"},
        "budget": {"level": "mid"},
        "themes": [],
        "hard_constraints": {},
    }
    try:
        plan = refine.solve_refine(intent, atoms, registry_entry, today=when.date())
    except Exception as e:
        out["note"] = f"solve_refine failed: {type(e).__name__}: {e}"
        return out
    atom_by_id = {a.get("id"): a for a in atoms}
    base = when.date()
    for day in plan.get("days", []):
        day_date = base + timedelta(days=day["day"] - 1)
        for item in day.get("items", []):
            if item.get("type") != "atom" or not item.get("atom_id"):
                continue
            atom = atom_by_id.get(item["atom_id"])
            if not atom:
                continue
            hh, mm = item["start"].split(":")
            start_dt = datetime.combine(day_date, datetime.min.time()) \
                .replace(hour=int(hh), minute=int(mm))
            res = gate.check_atom(atom, start_dt)
            out["checked"] += 1
            if res["status"] == "closed":
                out["closed_items"].append({
                    "day": day["day"], "slot": item.get("slot"),
                    "atom_id": item["atom_id"], "name_zh": item.get("name_zh"),
                    "start": item["start"], "detail": res["detail"][:160],
                })
    out["available"] = True
    return out


def audit_dest(dest: str, when: datetime, sample_n: int, registry_entry: dict) -> dict:
    atoms = _load_atoms(dest)
    gate.load_osm_index(dest)  # 确保索引存在

    with_oh, values = [], []
    for a in atoms:
        v, _src = gate.opening_hours_of(a, dest)
        if v is not None:
            with_oh.append(a)
            values.append(v)
    cov = oh.coverage_report(values)

    # 抽样校验：有数据的原子等距取样 sample_n 个
    samples = []
    if with_oh:
        step = max(1, len(with_oh) // sample_n)
        for a in with_oh[::step][:sample_n]:
            res = gate.check_atom(a, when)
            samples.append({
                "atom_id": a.get("id"),
                "name_zh": a.get("name", {}).get("zh"),
                "at": when.isoformat(timespec="minutes"),
                "status": res["status"], "source": res["source"],
                "detail": res["detail"][:140],
            })

    plan_checks = _sample_plan_checks(dest, atoms, registry_entry, when)

    return {
        "dest": dest,
        "atom_count": len(atoms),
        "with_opening_hours": len(with_oh),
        "oh_coverage_pct": round(len(with_oh) / len(atoms) * 100, 1) if atoms else 0.0,
        "parser_coverage_on_atom_values": cov,
        "sample_checks": samples,
        "sample_plan": plan_checks,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="营业状态校验门审计（BACKLOG #2）")
    ap.add_argument("--dest", help="目的地；缺省全部")
    ap.add_argument("--when", default=None,
                    help="ISO 时刻，如 2026-09-15T15:00（缺省：下周二 15:00）")
    ap.add_argument("--sample", type=int, default=10)
    ap.add_argument("--out", help="把报告写到 JSON 文件")
    args = ap.parse_args()

    if args.when:
        when = datetime.fromisoformat(args.when)
    else:
        # 缺省：下周二 15:00（避开周一闭馆高频日，演示更有区分度）
        today = date.today()
        days_ahead = (1 - today.weekday()) % 7 or 7
        when = datetime.combine(today + timedelta(days=days_ahead),
                                datetime.min.time()).replace(hour=15)

    registry = _load_registry()
    entries = {d["id"]: d for d in registry["destinations"]}
    dests = [args.dest] if args.dest else list(entries)
    reports = []
    for dest in dests:
        if dest not in entries:
            print(f"未知目的地: {dest}", file=sys.stderr)
            sys.exit(2)
        rep = audit_dest(dest, when, args.sample, entries[dest])
        reports.append(rep)
        cov = rep["parser_coverage_on_atom_values"]
        print(f"\n===== {dest} 营业状态审计 @ {when.isoformat(timespec='minutes')} =====")
        print(f"原子总数 {rep['atom_count']}，有 opening_hours 数据 "
              f"{rep['with_opening_hours']}（{rep['oh_coverage_pct']}%）")
        print(f"解析器覆盖率：{cov['parse_coverage'] * 100:.1f}% "
              f"（{cov['parsed']}/{cov['total']}，其中 24/7 {cov['of_which_24_7']} 条）")
        print(f"抽样校验 {len(rep['sample_checks'])} 条：")
        for s in rep["sample_checks"]:
            print(f"  [{s['status']:>7}] {s['name_zh']} ({s['atom_id']}) — {s['detail']}")
        sp = rep["sample_plan"]
        if sp["available"]:
            print(f"样本方案校验：检查 {sp['checked']} 个 atom 槽位，"
                  f"闭店命中 {len(sp['closed_items'])} 个")
            for it in sp["closed_items"]:
                print(f"  ✗ day{it['day']} {it['slot']} {it['start']} "
                      f"{it['name_zh']} ({it['atom_id']}) — {it['detail']}")
        else:
            print(f"样本方案校验跳过：{sp['note']}")

    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"when": when.isoformat(timespec="minutes"),
                       "reports": reports}, f, ensure_ascii=False, indent=1)
        print(f"\n报告已写入 {args.out}")


if __name__ == "__main__":
    main()
