"""packages/verify —— 营业状态/开放时间实时校验门（BACKLOG #2，P0）。

外部调研结论：推荐已关闭场所是信任头号杀手。本包为 solver 提供确定性的
「这个原子在这个时刻开不开」校验门，数据源为 OSM opening_hours 标签
（来自 data/raw/overpass_*.json 真实抓取缓存）。

组成：
- opening_hours.py：OSM opening_hours 标签务实解析器（覆盖真实数据 ~96%，
  解析不出一律 unknown，绝不猜测）。
- gate.py：check_atom(atom, when) 校验门 + OSM 原始缓存索引
  （data/cache/opening_hours_index_{dest}.json，由 audit 或首次使用时构建）。
- audit.py：`python3 -m packages.verify.audit --dest osaka` 审计报告。

★ solver 集成点（接口约定；实际接线由 solver 所有者完成）★

    from packages.verify.gate import check_atom

    result = check_atom(atom, when, holidays=None)
    # atom: 原子 dict（data/atoms/{dest}.json 元素）
    # when: datetime（该原子在方案中的计划开始时刻，本地时区语义由调用方保证）
    # holidays: 可选 set[date] 公共假期表；缺省时涉及 PH 语义的结论返回 unknown
    # 返回 {"status": "open"|"closed"|"unknown",
    #       "source": "atom_facts"|"osm_raw_cache"|"none",
    #       "detail": str}

调用时机建议：
1. refine/resolve 选点阶段：对每个候选原子在其目标槽位时刻调用一次；
   status=="closed" → 硬过滤（不进候选池，记入 excluded，理由可进收据：
   "周二闭馆所以排周三" 这类真实理由）；
2. status=="unknown" → 不惩罚、不放行加权，仅在 risk_flags 追加
   "opening_hours_unknown" 供呈现层提示"营业时间待核实"；
3. 校验是纯本地、纯确定性（读本地缓存），可安全放在请求路径上；
   网络抓取刷新 opening_hours 属 atomizer/数据管线职责，不在本包。

注意：本包不修改 data/atoms/*.json。若 atomizer 未来把 opening_hours 持久化进
原子（可选字段 facts.opening_hours_raw，非破坏性新增），gate 会优先读取该字段；
当前回落路径是按 facts.source 里的 osm:{type}/{id} 匹配 data/raw 缓存。
"""
from packages.verify.gate import check_atom, build_osm_index, load_osm_index
from packages.verify.opening_hours import parse, evaluate, coverage_report

__all__ = ["check_atom", "build_osm_index", "load_osm_index",
           "parse", "evaluate", "coverage_report"]
