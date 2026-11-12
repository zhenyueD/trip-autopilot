"""L1 意图化叙事层（ADR-002 §2.2 / V1.1 PRD P3）。

纪律：
- 只动表达层：事实层永远来自 L0 原子字段 / receipts 模板；
- 事实一致性后置校验：叙事中的数字/时间必须有 L0 出处，无出处 → 自动重写一次
  → 仍失败 → 该项回退模板收据文案（用户永远拿得到方案）；
- 顾问语气（讨论稿 §1）：具体、有主见、至少引用一个真实事实，禁止空话；
- 成本：一次批量调用写全部入选原子（+ 必要时一次重写），指纹缓存命中不调。
"""
from .narrate import narrate_plan
from .chat import chat_modify

__all__ = ["narrate_plan", "chat_modify"]
