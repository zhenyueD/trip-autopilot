"""packages/solver —— V1 求解器（docs/V1-BUILD.md §3）。

- skeleton.py：骨架求解（硬过滤 → 聚类分日 → 门户进出）
- refine.py：槽位填充 + 餐饮伪 POI + 硬节奏约束 + 全日排线
- resolve.py：局部重解（换一个）：稳定目标 + pin + 日级范围 + diff
- receipts.py：模板化决策收据（真实性铁律：只引用真实绑定的约束）
- compare.py：§6 对比实验（有/无预处理）
- common.py：日程时间模型与硬节奏约束常量（refine/resolve 共用）
"""
