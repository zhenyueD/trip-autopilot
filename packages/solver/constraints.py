"""约束编译器（BACKLOG #9）：IntentVector → {hard_predicates, soft_weights}。

设计原则：硬约束剪枝（prune），永不进目标函数降分；软权重只影响排序。
所有 solve 路径（refine 选点 / resolve 换原子）共享本模块作为唯一约束事实源。

硬谓词清单（返回 None = 通过，否则返回排除原因字符串）：
1. destination_mismatch        —— 目的地不匹配（matcher）
2. physical_level              —— 体力上限（matcher）
3. unsuitable_for              —— 人群不适合（matcher）
4. budget_tier                 —— hard_cap=true 时预算档一票否决；
                                 hard_cap=false 时放宽一档（matcher 原有行为，语义保留）
5. explicit_exclusion          —— 显式排除谓词（matcher）
6. booking_window_missed       —— booking_required=required 且 出发日-today < lead_days
                                 → 窗口已错过，直接剪枝（原为 risk_flag，升级为硬约束）
7. transit_forbidden           —— 两槽位间通行 >75min（特种兵豁免），
                                 在 layout_day 布局期逐边校验（带位置上下文，不在本表）
8. cluster_per_day             —— 每天绑定 1 个 cluster（结构性，由 skeleton 分日保证）
9. pacing buffers              —— 每半天 ≥30min 空档 / 连续活动 ≤4h（结构性，layout 保证）

软权重：weights.effective_weights(intent)（base WEIGHTS + persona 增量），
只进 score_atom 与 sequence.path_delta，永不用于剪枝。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from packages.intent.matcher import hard_filter_reason
from packages.intent import weights as wts


@dataclass
class CompiledConstraints:
    """一份编译好的约束：solve 路径共享的事实源。"""

    hard_predicate_names: list[str]          # 文档化清单（顺序即评估顺序）
    soft_weights: dict                       # matcher.score_atom 的有效权重
    sequence_params: dict                    # sequence.py 的序列惩罚参数
    transit_forbidden_min: int               # 通行禁则阈值（特种兵放宽为无穷大）
    commando: bool
    atoms_per_day: int
    _intent: dict = field(repr=False)
    _today: date = field(repr=False)

    def hard_reason(self, atom: dict) -> Optional[str]:
        """硬谓词合取：任一不通过即返回原因（剪枝），全部通过返回 None。"""
        # 1-5：matcher 硬过滤契约（destination/体力/人群/预算/显式排除）
        reason = hard_filter_reason(self._intent, atom)
        if reason is not None:
            return reason
        # 6：预约窗口已错过 → 剪枝（原为 risk_flag 降级提示，编译器升级为硬约束）
        c = atom.get("constraints", {})
        if c.get("booking_required") == "required":
            rng = self._intent.get("time", {}).get("departure_date_range")
            if rng:
                days_to_depart = (date.fromisoformat(rng[0]) - self._today).days
                lead = c.get("booking_lead_days", 0)
                if days_to_depart < lead:
                    return (f"booking_window_missed:距出发{days_to_depart}天"
                            f"<需提前{lead}天，窗口已错过")
        return None


HARD_PREDICATES = [
    "destination_mismatch", "physical_level", "unsuitable_for",
    "budget_tier(hard_cap)", "explicit_exclusion", "booking_window_missed",
    "transit_forbidden(layout)", "cluster_per_day(structural)", "pacing_buffers(structural)",
]


def compile_constraints(intent: dict, today: Optional[date] = None) -> CompiledConstraints:
    """编译入口：IntentVector → CompiledConstraints（确定性，无副作用）。"""
    today = today or date.today()
    commando = intent.get("pace", {}).get("atoms_per_day", 3) >= 4
    return CompiledConstraints(
        hard_predicate_names=list(HARD_PREDICATES),
        soft_weights=wts.effective_weights(intent),
        sequence_params=wts.sequence_params(intent),
        transit_forbidden_min=10 ** 9 if commando else 75,
        commando=commando,
        atoms_per_day=intent.get("pace", {}).get("atoms_per_day", 3),
        _intent=intent,
        _today=today,
    )
