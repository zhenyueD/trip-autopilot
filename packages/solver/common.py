"""日程时间模型与硬节奏约束（V1-BUILD §3「硬节奏约束」）。

refine 与 resolve 共用同一套常量与半天窗口定义，保证验收断言的口径唯一。

时间单位：分钟（自 00:00 起）。半天窗口定义：
- AM = [day_start, 13:00) ；PM = [13:00, 21:30]
- 每半天空档缓冲 = 窗口长度 − (原子停留 + 通行) ；用餐与休息窗不计入占用（用餐本身即休整）
硬约束：
- 每天原子数 ≤ pace.atoms_per_day（默认 3）
- 每半天空档 ≥ BUFFER_MIN_PER_HALF_DAY（30min）
- 连续活动（停留+通行）> MAX_CONTINUOUS_MIN（4h）→ 强制休息窗 REST_WINDOW_MIN
- 两槽位间通行 > TRANSIT_PENALTY_MIN（40）降分；> TRANSIT_FORBIDDEN_MIN（75）禁止，
  除非特种兵步调（atoms_per_day ≥ COMMANDO_ATOMS_PER_DAY）。门户进出段豁免（结构性通勤）。
"""
from __future__ import annotations

DAY_START_DEFAULT = 9 * 60        # 09:00
DAY_START_EARLY = 8 * 60          # early_start_ok 时 08:00
HALF_DAY_SPLIT = 13 * 60          # 13:00：AM/PM 分界
DAY_END = 21 * 60 + 30            # 21:30

BUFFER_MIN_PER_HALF_DAY = 30
MAX_CONTINUOUS_MIN = 4 * 60
REST_WINDOW_MIN = 30
TRANSIT_PENALTY_MIN = 40
TRANSIT_FORBIDDEN_MIN = 75
COMMANDO_ATOMS_PER_DAY = 4

# 槽位锚点（布局时的最早开始时刻）
SLOT_ANCHOR = {
    "morning": DAY_START_DEFAULT,
    "noon": 11 * 60 + 30,
    "afternoon": 14 * 60,
    "sunset": 17 * 60,
    "evening": 18 * 60 + 30,
    "night": 20 * 60,
}

LUNCH_ANCHOR = 12 * 60
LUNCH_DURATION = 60
DURATION_DINNER = 75
DINNER_ANCHOR = 19 * 60

SLOT_ZH = {
    "morning": "上午", "noon": "中午", "afternoon": "下午",
    "sunset": "傍晚", "evening": "晚上", "night": "夜里",
}


def fmt_time(minutes: float) -> str:
    m = int(round(minutes))
    return f"{m // 60:02d}:{m % 60:02d}"


def half_day_of(start_min: float) -> str:
    return "am" if start_min < HALF_DAY_SPLIT else "pm"


def half_day_span(which: str, day_start: int) -> int:
    if which == "am":
        return HALF_DAY_SPLIT - day_start
    return DAY_END - HALF_DAY_SPLIT


def is_commando_pace(intent: dict) -> bool:
    return intent.get("pace", {}).get("atoms_per_day", 3) >= COMMANDO_ATOMS_PER_DAY


def day_start_of(intent: dict) -> int:
    return DAY_START_EARLY if intent.get("pace", {}).get("early_start_ok") else DAY_START_DEFAULT
