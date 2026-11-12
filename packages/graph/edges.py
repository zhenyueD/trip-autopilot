"""物理边：通行时间矩阵 + 缓存（V1-BUILD §2 边）。

铁律：请求路径上禁止外部路由 API。查询顺序（全部本地、确定性）：
1. OSRM 真实路网缓存 data/cache/osrm/{dest}.json（BACKLOG #5，
   由 packages/graph/build_matrices.py 离线预计算填充，条目带
   source: osrm|haversine_estimate 标记）；
2. 本文件的 haversine 懒缓存 data/cache/travel_matrix_{dest}.json；
3. haversine × 模式系数现场估算：
   - < WALK_MAX_KM → 步行 5 km/h
   - 否则 → 市内轨道等效 25 km/h（含换乘惩罚 TRANSIT_TRANSFER_PENALTY_MIN）

时段边（best_time_of_day × 槽位兼容）与情绪边（节奏规则）的确定性矩阵也在这里，
权重数值一律读 packages/intent/weights.py（唯一事实源），本文件不另立权重。
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile

WALK_SPEED_KMH = 5.0
TRANSIT_EFFECTIVE_KMH = 25.0        # 市内轨道含候车的等效速度
TRANSIT_TRANSFER_PENALTY_MIN = 10   # 换乘/进出站惩罚
WALK_MAX_KM = 1.2                   # 超过此距离不再步行

# ---- 时段边：best_time_of_day → 日程槽位兼容矩阵（确定性） ----
SLOT_ORDER = ["morning", "noon", "afternoon", "sunset", "evening", "night"]
SLOT_COMPAT = {
    "early_morning": {"morning"},
    "morning": {"morning"},
    "noon": {"noon", "afternoon"},
    "afternoon": {"afternoon"},
    "sunset": {"sunset", "afternoon"},
    "evening": {"evening", "sunset"},
    "night": {"night", "evening"},
    "any": set(SLOT_ORDER),
}

# ---- 情绪边：节奏规则（数值权重在 packages/intent/weights.py，此处只有结构规则） ----
# 连续同类别惩罚 / 动静交替加分的"结构"：由 solver 在选原子时按类别重复度施加，
# 重复度系数引用 weights.py 的 emotion_overlap 量级，不在此硬编码数值。


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class TravelMatrix:
    """通行时间矩阵：懒构建 + 持久化缓存。请求路径零外部调用。"""

    def __init__(self, dest: str, cache_dir: str = "data/cache"):
        self.dest = dest
        self.path = os.path.join(cache_dir, f"travel_matrix_{dest}.json")
        self._m: dict[str, float] = {}
        self._dirty = False
        self._osrm = None          # 懒加载的 routing.RoutingCache（只读）
        self._osrm_tried = False
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._m = data
            except (OSError, ValueError) as e:
                # 缓存损坏（并发写截断/手动改坏）：告警并当空缓存处理，不崩启动
                print(f"[TravelMatrix] 缓存损坏，按空缓存处理: {self.path} ({e})",
                      file=sys.stderr)

    def _osrm_lookup(self, a: tuple[float, float], b: tuple[float, float]) -> dict | None:
        """OSRM 预计算缓存只读查询（BACKLOG #5）。无缓存/无条目返回 None。"""
        if not self._osrm_tried:
            self._osrm_tried = True
            try:
                from packages.graph import routing
                cache = routing.RoutingCache(self.dest)
                self._osrm = cache if cache.entries else None
            except ImportError:
                # routing 依赖 requests（仅离线预计算用）；无 requests 的环境
                # 静默回退 haversine —— 请求路径纪律保持不变
                self._osrm = None
            except Exception as e:
                # 缓存损坏/权限等其他异常：告警后回退，不静默吞掉
                print(f"[TravelMatrix] OSRM 缓存不可用，回退 haversine: {e}",
                      file=sys.stderr)
                self._osrm = None
        if self._osrm is None:
            return None
        return self._osrm.get(a, b)

    @staticmethod
    def _key(a: tuple[float, float], b: tuple[float, float]) -> str:
        # 坐标 6 位小数足以区分城市内任意两点；对称键（A|B 与 B|A 同一值）
        ka = f"{a[0]:.6f},{a[1]:.6f}"
        kb = f"{b[0]:.6f},{b[1]:.6f}"
        return f"{ka}|{kb}" if ka <= kb else f"{kb}|{ka}"

    @staticmethod
    def estimate_minutes(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float, str]:
        """(minutes, km, mode)。haversine × 模式系数，确定性。"""
        km = haversine_km(a[0], a[1], b[0], b[1])
        if km <= WALK_MAX_KM:
            return round(km / WALK_SPEED_KMH * 60, 1), round(km, 3), "walk"
        minutes = km / TRANSIT_EFFECTIVE_KMH * 60 + TRANSIT_TRANSFER_PENALTY_MIN
        return round(minutes, 1), round(km, 3), "transit"

    def minutes(self, a: tuple[float, float], b: tuple[float, float]) -> float:
        hit = self._osrm_lookup(a, b)
        if hit is not None:
            return hit["minutes"]
        k = self._key(a, b)
        if k not in self._m:
            m, _, _ = self.estimate_minutes(a, b)
            self._m[k] = m
            self._dirty = True
        return self._m[k]

    def leg(self, a: tuple[float, float], b: tuple[float, float]) -> dict:
        """完整边信息：分钟 / 公里 / 模式 / 数据来源（分钟走缓存）。

        source: "osrm"（真实路网缓存命中）| "haversine_estimate"（估算）。
        OSRM 命中时 km 用缓存里的真实路网里程，与 minutes 自洽；
        mode 按直线距离判断（步行/轨道口径不变）。
        """
        hit = self._osrm_lookup(a, b)
        if hit is not None:
            hav_km = haversine_km(a[0], a[1], b[0], b[1])
            mode = "walk" if hav_km <= WALK_MAX_KM else "transit"
            return {"minutes": hit["minutes"],
                    "km": round(hit.get("km") or hav_km, 3),
                    "mode": mode,
                    "source": hit.get("source", "osrm")}
        minutes = self.minutes(a, b)
        km = haversine_km(a[0], a[1], b[0], b[1])
        mode = "walk" if km <= WALK_MAX_KM else "transit"
        return {"minutes": minutes, "km": round(km, 3), "mode": mode,
                "source": "haversine_estimate"}

    def save(self) -> None:
        if not self._dirty:
            return
        dir_ = os.path.dirname(self.path)
        if dir_:
            os.makedirs(dir_, exist_ok=True)
        # 唯一临时文件 + os.replace：并发写下互不覆盖，替换本身原子
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(self.path) + ".",
                                   suffix=".tmp", dir=dir_ or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._m, f)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._dirty = False


def slot_compatible(best_time_of_day: list[str] | None, slot: str) -> bool:
    """时段边：原子的 best_time_of_day 是否兼容目标槽位。"""
    if not best_time_of_day:
        return True  # 缺省视为 any（schema 允许 null 风格缺省时的宽松兜底）
    for bt in best_time_of_day:
        if slot in SLOT_COMPAT.get(bt, set()):
            return True
    return False
