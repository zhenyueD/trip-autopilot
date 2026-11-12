"""packages/graph —— V1 最小图模型（docs/V1-BUILD.md §2）。

节点四类：atom（来自 data/atoms/*.json）、gateway（内置表）、
lodging_zone（由 cluster 中心性派生）、meal_slot（求解时生成，不入库）。
边 v1 最小集：物理边（haversine × 模式系数，缓存到 data/cache/）、
时段边（best_time_of_day × 槽位兼容矩阵）、情绪边（读 packages/intent/weights.py）。
请求路径上禁止任何外部路由 API。
"""
