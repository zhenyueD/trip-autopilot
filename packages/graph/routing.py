"""OSRM 真实路网客户端 + 持久化缓存（BACKLOG #5）。

设计要点：
- 目标服务：公共 demo 服务器 https://router.project-osrm.org（table/route 端点）。
  礼貌使用：全局限速 ≤1 req/s，429/5xx 指数退避重试，一切结果落盘缓存，
  重复运行零网络请求。
- 缓存：data/cache/osrm/{dest}.json，条目带 source 标记
  （"osrm" 真实路网 / "haversine_estimate" 离线兜底）。
- 模式近似（重要，见下）：
  * haversine ≤ WALK_MAX_KM(1.2km) 的短腿：不消耗 OSRM 配额，直接走
    edges.py 的步行估算（缓存里不出现）。
  * 簇内 1.2–3km 的腿：OSRM foot profile（步行路网）。
  * 跨簇 / 门户长腿：OSRM car(driving) profile 作为**市内轨道/出租车的代理**。
    这是有意的近似：OSRM 没有公共交通 profile。大阪地铁网的门到门时间
    通常介于驾车与驾车×1.5 之间；阿布扎比无轨交、出行为主，driving 最接近真实。
    Transitous（真实公交 GTFS-RT）接入是未来工作，见 TransitProvider 接口。
- 对称键：A→B 与 B→A 存同一键（同 edges.py TravelMatrix 口径）。
  城市内单程方向差通常 <10%，可接受；如需方向精度未来把键改为有向。

铁律不变：请求路径上仍然禁止实时外部调用 —— TravelMatrix 只读本地缓存；
本模块的网络访问只发生在 build_matrices 预计算阶段（或显式 offline=False 的调用方）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

import requests

OSRM_BASE = "https://router.project-osrm.org"

# 模式切换阈值（km，haversine 直线距离）
FOOT_MAX_KM = 3.0          # ≤3km 且 >1.2km 的簇内腿用 foot profile
MIN_INTERVAL_S = 1.1       # 公共 demo 服务器礼貌限速：≤1 req/s（留 10% 余量）
MAX_RETRIES = 3
BACKOFF_BASE_S = 2.0
TIMEOUT_S = 30

PROFILE_FOOT = "foot"
PROFILE_DRIVING = "driving"   # OSRM demo 端点名是 "car"；本包内部统一叫 driving

_OSRM_SERVICE = {PROFILE_FOOT: "foot", PROFILE_DRIVING: "car"}

SOURCE_OSM = "osrm"
SOURCE_HAVERSINE = "haversine_estimate"

CACHE_DIR = os.path.join("data", "cache", "osrm")


def _pair_key(a: tuple[float, float], b: tuple[float, float]) -> str:
    """对称坐标对键（与 edges.TravelMatrix._key 同口径，6 位小数）。"""
    ka = f"{a[0]:.6f},{a[1]:.6f}"
    kb = f"{b[0]:.6f},{b[1]:.6f}"
    return f"{ka}|{kb}" if ka <= kb else f"{kb}|{ka}"


def profile_for_km(km: float) -> str:
    """按直线距离选 OSRM profile。≤1.2km 的腿不应走到这里（走 haversine）。

    ≤3km → foot（步行路网）；>3km → driving（市内轨道/出租车代理）。
    """
    return PROFILE_FOOT if km <= FOOT_MAX_KM else PROFILE_DRIVING


class RoutingCache:
    """data/cache/osrm/{dest}.json 持久化缓存。"""

    def __init__(self, dest: str, cache_dir: str = CACHE_DIR):
        self.dest = dest
        self.path = os.path.join(cache_dir, f"{dest}.json")
        self.entries: dict[str, dict] = {}
        self._dirty = False
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                self.entries = data.get("entries", {})
            except (OSError, ValueError, AttributeError) as e:
                # 缓存损坏：告警并当空缓存处理（build_matrices 会重新填充）
                print(f"[RoutingCache] 缓存损坏，按空缓存处理: {self.path} ({e})",
                      file=sys.stderr)
                self.entries = {}

    @staticmethod
    def key(profile: str, a: tuple[float, float], b: tuple[float, float]) -> str:
        return f"{profile}|{_pair_key(a, b)}"

    def get(self, a: tuple[float, float], b: tuple[float, float],
            profile: str | None = None) -> dict | None:
        """查缓存。指定 profile 精确查；未指定时任意 profile 命中即返回。"""
        if profile:
            return self.entries.get(self.key(profile, a, b))
        for prof in (PROFILE_DRIVING, PROFILE_FOOT):
            hit = self.entries.get(self.key(prof, a, b))
            if hit is not None:
                return hit
        return None

    def put(self, profile: str, a: tuple[float, float], b: tuple[float, float],
            minutes: float, km: float, source: str) -> None:
        self.entries[self.key(profile, a, b)] = {
            "minutes": round(minutes, 1),
            "km": round(km, 3),
            "source": source,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        dir_ = os.path.dirname(self.path)
        if dir_:
            os.makedirs(dir_, exist_ok=True)
        payload = {
            "meta": {
                "dest": self.dest,
                "entry_count": len(self.entries),
                "note": "driving profile = 市内轨道/出租车代理；Transitous 真实公交为未来工作",
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            "entries": self.entries,
        }
        # 唯一临时文件 + os.replace：并发写互不覆盖，替换原子
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(self.path) + ".",
                                   suffix=".tmp", dir=dir_ or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._dirty = False

    def stats(self) -> dict:
        n_osrm = sum(1 for e in self.entries.values() if e["source"] == SOURCE_OSM)
        return {"total": len(self.entries), "osrm": n_osrm,
                "haversine_estimate": len(self.entries) - n_osrm}


class OSRMClient:
    """公共 demo 服务器的礼貌客户端：限速 + 退避重试。断网时所有方法返回 None。"""

    def __init__(self, base_url: str = OSRM_BASE,
                 min_interval_s: float = MIN_INTERVAL_S,
                 offline: bool = False):
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.offline = offline
        self._last_request_at = 0.0
        self._session = requests.Session()
        self.requests_made = 0
        self.available: bool | None = None  # None=未探测 True/False=最近一次探测结果

    def _throttle(self) -> None:
        wait = self.min_interval_s - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        """GET with 限速 + 429/5xx/网络错误退避重试。彻底失败返回 None。"""
        if self.offline:
            return None
        for attempt in range(MAX_RETRIES + 1):
            self._throttle()
            try:
                resp = self._session.get(f"{self.base_url}{path}",
                                         params=params, timeout=TIMEOUT_S)
                self.requests_made += 1
                self._last_request_at = time.monotonic()
            except requests.RequestException:
                self._last_request_at = time.monotonic()
                if attempt < MAX_RETRIES:
                    time.sleep(BACKOFF_BASE_S * (2 ** attempt))
                    continue
                self.available = False
                return None
            if resp.status_code == 200:
                self.available = True
                try:
                    return resp.json()
                except ValueError:
                    return None
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < MAX_RETRIES:
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else BACKOFF_BASE_S * (2 ** attempt)
                    except ValueError:
                        delay = BACKOFF_BASE_S * (2 ** attempt)
                    time.sleep(min(delay, 60.0))
                    continue
                self.available = False
                return None
            # 4xx（非 429）：请求本身有问题，不重试
            return None
        return None

    def table(self, coords: list[tuple[float, float]],
              profile: str = PROFILE_DRIVING) -> dict | None:
        """table 端点：一次请求拿到 N×N 时长/距离矩阵。

        返回 {"durations_min": [[...]], "distances_km": [[...]]}；失败返回 None。
        demo 服务器 table 上限约 100 坐标，调用方负责分批。
        """
        if len(coords) < 2:
            return None
        service = _OSRM_SERVICE[profile]
        coord_str = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coords)
        data = self._get(f"/table/v1/{service}/{coord_str}",
                         params={"annotations": "duration,distance"})
        if not data or data.get("code") != "Ok":
            return None
        durations = data.get("durations")
        distances = data.get("distances")
        if not durations:
            return None
        n = len(coords)
        dur_min = [[(durations[i][j] / 60.0 if durations[i][j] is not None else None)
                    for j in range(n)] for i in range(n)]
        dist_km = None
        if distances:
            dist_km = [[(distances[i][j] / 1000.0 if distances[i][j] is not None else None)
                        for j in range(n)] for i in range(n)]
        return {"durations_min": dur_min, "distances_km": dist_km}

    def route(self, a: tuple[float, float], b: tuple[float, float],
              profile: str = PROFILE_DRIVING) -> dict | None:
        """route 端点：单腿查询。返回 {"minutes":…, "km":…}；失败返回 None。"""
        service = _OSRM_SERVICE[profile]
        path = (f"/route/v1/{service}/{a[1]:.6f},{a[0]:.6f};"
                f"{b[1]:.6f},{b[0]:.6f}")
        data = self._get(path, params={"overview": "false"})
        if not data or data.get("code") != "Ok" or not data.get("routes"):
            return None
        r = data["routes"][0]
        return {"minutes": r["duration"] / 60.0, "km": r["distance"] / 1000.0}


def lookup_cached(dest: str, a: tuple[float, float], b: tuple[float, float],
                  cache_dir: str = CACHE_DIR) -> dict | None:
    """给 edges.TravelMatrix 用的只读缓存查询。无缓存返回 None。"""
    cache = RoutingCache(dest, cache_dir)
    return cache.get(a, b)


class TransitProvider:
    """真实公共交通时间提供者接口（未来工作：Transitous / GTFS-RT）。

    未来实现（如 TransitousClient）应满足：

        class TransitousClient(TransitProvider):
            def minutes(self, a: tuple[float, float], b: tuple[float, float],
                        when: datetime) -> float | None: ...

    语义：返回门到门分钟数（含候车/换乘），无覆盖返回 None（调用方回退
    driving 代理或 haversine）。集成点：build_matrices 在跨簇长腿上优先
    调用 TransitProvider，失败再退到 OSRM driving —— 调用处已按
    「provider 可插拔」组织，接入时无需改缓存格式（条目 source 增加
    "transitous" 枚举值即可）。
    """

    def minutes(self, a: tuple[float, float], b: tuple[float, float],
                when: datetime) -> float | None:
        raise NotImplementedError("Transitous 接入是未来工作（BACKLOG #5 备注）")
