"""公共配置与工具函数。"""
from __future__ import annotations

import math
import re
import time
import unicodedata
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---- 路径 ----
WORKSPACE = Path(__file__).resolve().parents[2]
DATA_RAW = WORKSPACE / "data" / "raw"
DATA_INTERIM = WORKSPACE / "data" / "interim"
DATA_ATOMS = WORKSPACE / "data" / "atoms"
for _d in (DATA_RAW, DATA_INTERIM, DATA_ATOMS):
    _d.mkdir(parents=True, exist_ok=True)

# ---- 目的地配置 ----
# bbox = (south, west, north, east)
DESTINATIONS = {
    "osaka": {
        "bbox": (34.60, 135.40, 34.80, 135.65),
        "wv_prefix": "Osaka/",       # Wikivoyage 分区页面前缀
        "wv_root": "Osaka",
        "center": (34.6937, 135.5023),
        "name_zh": "大阪",
        "currency": "JPY",
        "timezone": "Asia/Tokyo",
        "price_field": "price_hint_jpy",
        "cap": 450,                  # 入库上限（质量优先）
        "target": 200,
    },
    "bangkok": {
        "bbox": (13.60, 100.40, 13.90, 100.65),
        "wv_prefix": "Bangkok/",
        "wv_root": "Bangkok",
        "center": (13.7563, 100.5018),
        "name_zh": "曼谷",
        "currency": "THB",
        "timezone": "Asia/Bangkok",
        "price_field": "price_hint_thb",
        "cap": 400,
        "target": 150,
    },
    "abudhabi": {
        # 覆盖 downtown Corniche / Saadiyat / Yas Island / 大清真寺
        "bbox": (24.30, 54.25, 24.60, 54.80),
        "wv_prefix": "Abu Dhabi/",   # Wikivoyage 无子分区页，仅用根页面（自动降级）
        "wv_root": "Abu Dhabi",
        "center": (24.4539, 54.3773),
        "name_zh": "阿布扎比",
        "currency": "AED",
        "timezone": "Asia/Dubai",
        "price_field": "price_hint_aed",
        "cap": 300,
        "target": 150,
    },
}

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
WIKIVOYAGE_API = "https://en.wikivoyage.org/w/api.php"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# overpass-api.de 对浏览器 UA 返回 406，OSM 社区惯例是带项目标识的 UA
OVERPASS_UA = "travel-agent-atomizer/0.1 (open-data POI pipeline; contact: local dev)"


def make_session(ua: str = USER_AGENT) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": ua, "Accept": "application/json"})
    retry = Retry(
        total=3, connect=3, read=3, backoff_factor=2.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


# ---- 地理 ----
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---- 文本 ----
def norm_name(name: str | None) -> str:
    if not name:
        return ""
    name = unicodedata.normalize("NFKC", name).strip().lower()
    name = re.sub(r"[\s　]+", "", name)
    name = re.sub(r"[()（）\[\]【】]", "", name)
    return name


def slugify(name: str | None) -> str:
    """英文名 → 小写下划线 slug。非 ASCII 返回空串。"""
    if not name:
        return ""
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    return re.sub(r"_+", "_", name)


def today_iso() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())
