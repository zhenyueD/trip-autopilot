"""启发式解析器：中文关键词 / 正则 → IntentVector。

always-available 降级路径（spec §6：无 LLM 可用时必须独立可用）。
职责边界：只做"理解"（文本 → 向量），匹配/排序/求解全在 matcher.py 的确定性代码里。

推断纪律（spec §2/§3）：
- 用户直接说出的字段（目的地、N 天、人均 X 元）→ 显式，不进 inferred；
- 由画像/常识推导的字段（带爸妈 → mobility ≤ 2；暖和 → min_day_temp_c ≥ 15）→ 进 inferred，
  呈现层对这些字段展示"我猜的？点我改"；
- 关键缺失字段（目的地 / 日期 / 预算）→ 进 uncertainty，一句自然语言问题 + ≤4 个可点选项。
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional

from .schema import default_intent_vector

# ---- 目的地词典（V1 做深大阪 + 曼谷，其余地名先识别出来进向量，供后续扩容） ----

DESTINATIONS = {
    "大阪": "osaka", "京都": "kyoto", "东京": "tokyo", "奈良": "nara",
    "神户": "kobe", "曼谷": "bangkok", "清迈": "chiang_mai", "迪拜": "dubai",
    "阿布扎比": "abudhabi",
    "首尔": "seoul", "新加坡": "singapore", "芭提雅": "pattaya", "普吉": "phuket",
}

# 春节（CNY）日期表：用于"过年/春节"的日期区间推断。确定性事实，过期需人工更新。
_CNY_DATES = [
    date(2026, 2, 17), date(2027, 2, 6), date(2028, 1, 26),
    date(2029, 2, 13), date(2030, 2, 3),
]

_CN_NUM = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_int(s: str) -> Optional[int]:
    """极简中文数字转换：支持 十 / 十五 / 二十 / 二十五 及个位数。"""
    if s.isdigit():
        return int(s)
    if not s or any(c not in _CN_NUM for c in s):
        return None
    if "十" in s:
        left, _, right = s.partition("十")
        tens = _CN_NUM[left] if left else 1
        ones = _CN_NUM[right] if right else 0
        return tens * 10 + ones
    if len(s) == 1:
        return _CN_NUM[s]
    return None


def _mark_inferred(vec: dict, field: str) -> None:
    if field not in vec["inferred"]:
        vec["inferred"].append(field)


def _add_unique(lst: list, item: str) -> None:
    if item not in lst:
        lst.append(item)


def _next_window(today: date, start_md: tuple[int, int], end_md: tuple[int, int],
                 end_next_year: bool) -> list[str]:
    """计算"下一个"季节性窗口 [start, end]，跨年窗口由 end_next_year 标记。

    跨年窗口（如冬季 12/15–次年 2/15）的开始年份可以是去年：1–2 月说"冬天去"
    指当下正在进行的冬季窗口，而不是 11 个月后的下一个（off-by-one 修复）。
    """
    sm, sd = start_md
    em, ed = end_md
    years = (today.year - 1, today.year, today.year + 1) if end_next_year \
        else (today.year, today.year + 1)
    for y in years:
        start = date(y, sm, sd)
        end = date(y + 1, em, ed) if end_next_year else date(y, em, ed)
        if end >= today:
            return [start.isoformat(), end.isoformat()]
    start = date(today.year + 1, sm, sd)
    end = date(today.year + 2, em, ed) if end_next_year else date(today.year + 1, em, ed)
    return [start.isoformat(), end.isoformat()]


def parse(text: str, today: Optional[date] = None) -> dict:
    """中文自然语言 → IntentVector（启发式路径）。始终返回 schema 合法的向量。"""
    today = today or date.today()
    vec = default_intent_vector(raw_input=text)
    explicit: set[str] = set()  # 记录显式字段，用于置信度

    # ---------- 同行人构成（spec §3：决定 suitable_for 过滤方向） ----------
    comp: list[str] = []
    if re.search(r"爸妈|父母|带老人|老人家|长辈|带妈|带爸", text):
        comp.append("elderly_parents")
        vec["party"]["mobility_max_physical_level"] = 2  # 带老人收紧体力上限
        _mark_inferred(vec, "party.mobility_max_physical_level")
        vec["pace"]["rest_afternoon"] = True
        _mark_inferred(vec, "pace.rest_afternoon")
    if re.search(r"情侣|对象|男朋友|女朋友|爱人|老公|老婆|蜜月|纪念日|另一半", text):
        comp.append("couple")
    if re.search(r"亲子|带孩子|带娃|小孩|孩子|宝宝|儿童|遛娃|儿子|女儿", text):
        comp.append("family_kids")
        if vec["party"]["mobility_max_physical_level"] > 3:
            vec["party"]["mobility_max_physical_level"] = 3
            _mark_inferred(vec, "party.mobility_max_physical_level")
    if re.search(r"一个人|独自|独行|自己一个|solo|单身旅行", text, re.IGNORECASE):
        comp.append("solo")
    if re.search(r"(?<![男女])朋友|闺蜜|哥们|兄弟|同事|团建|同学", text):
        comp.append("group_friends")
    if comp:
        vec["party"]["composition"] = comp
        explicit.add("party.composition")

    # ---------- 同行人数 ----------
    m = re.search(r"一家三口", text)
    if m:
        vec["party"]["size"] = 3
        explicit.add("party.size")
    else:
        m = re.search(r"([0-9]+|[一二两三四五六七八九十])\s*(?:个)?人(?:同行|一起|出行)?", text)
        if m:
            n = _cn_int(m.group(1))
            if n and n >= 1:
                vec["party"]["size"] = n
                explicit.add("party.size")
    if vec["party"]["size"] is None and comp:
        # 由构成推断默认人数（推断值，进 inferred）
        default_size = {"elderly_parents": 3, "couple": 2, "family_kids": 3,
                        "solo": 1, "group_friends": 4}
        vec["party"]["size"] = max(default_size[c] for c in comp)
        _mark_inferred(vec, "party.size")

    # ---------- 时长 ----------
    m = re.search(r"([0-9]+|[一二两三四五六七八九十]{1,3})\s*天(?:\s*([0-9]+|[一二两三四五六七八九十]{1,3})\s*晚)?", text)
    if m:
        days = _cn_int(m.group(1))
        if days and 1 <= days <= 60:
            vec["time"]["duration_days"] = days
            vec["time"]["flexible"] = False
            explicit.add("time.duration_days")
    elif re.search(r"周末(?:游|去|玩|两日游)?", text):
        vec["time"]["duration_days"] = 2
        _mark_inferred(vec, "time.duration_days")
    # "几日游"：有出行意图但天数未定 → 留在 uncertainty

    # ---------- 季节与日期区间 ----------
    if re.search(r"樱花", text):
        vec["time"]["season"] = "spring"
        # 樱花季是确定性常识窗口：关西（大阪/京都）3 月底 – 4 月初
        vec["time"]["departure_date_range"] = _next_window(today, (3, 25), (4, 10), False)
        vec["time"]["flexible"] = False
        _mark_inferred(vec, "time.departure_date_range")
        explicit.add("time.season")
    elif re.search(r"枫叶|红叶", text):
        vec["time"]["season"] = "autumn"
        vec["time"]["departure_date_range"] = _next_window(today, (11, 15), (12, 5), False)
        vec["time"]["flexible"] = False
        _mark_inferred(vec, "time.departure_date_range")
        explicit.add("time.season")
    elif re.search(r"清明", text):
        vec["time"]["season"] = "spring"
        vec["time"]["departure_date_range"] = _next_window(today, (4, 2), (4, 7), False)
        vec["time"]["flexible"] = False
        _mark_inferred(vec, "time.departure_date_range")
        explicit.add("time.season")
    elif re.search(r"端午", text):
        vec["time"]["season"] = "summer"
        vec["time"]["departure_date_range"] = _next_window(today, (5, 28), (6, 5), False)
        vec["time"]["flexible"] = False
        _mark_inferred(vec, "time.departure_date_range")
        explicit.add("time.season")
    elif re.search(r"国庆|十一", text):
        vec["time"]["season"] = "autumn"
        vec["time"]["departure_date_range"] = _next_window(today, (9, 28), (10, 8), False)
        vec["time"]["flexible"] = False
        _mark_inferred(vec, "time.departure_date_range")
        explicit.add("time.season")
    elif re.search(r"中秋", text):
        vec["time"]["season"] = "autumn"
        explicit.add("time.season")
    elif re.search(r"过年|春节", text):
        vec["time"]["season"] = "winter"
        explicit.add("time.season")
        for cny in _CNY_DATES:
            if cny + timedelta(days=4) >= today:
                vec["time"]["departure_date_range"] = [
                    (cny - timedelta(days=3)).isoformat(),
                    (cny + timedelta(days=4)).isoformat(),
                ]
                vec["time"]["flexible"] = False
                _mark_inferred(vec, "time.departure_date_range")
                break
        # CNY 表只维护到 2030（表头有注释）：超出覆盖时保持日期空缺、显式 flexible=True，
        # 由 uncertainty 问日期——不产出"季节已知但日期缺失且不灵活"的矛盾向量
        if vec["time"]["departure_date_range"] is None:
            vec["time"]["flexible"] = True
    elif re.search(r"冬天|冬季|寒假|冬日", text):
        vec["time"]["season"] = "winter"
        vec["time"]["departure_date_range"] = _next_window(today, (12, 15), (2, 15), True)
        vec["time"]["flexible"] = False
        _mark_inferred(vec, "time.departure_date_range")
        explicit.add("time.season")
    elif re.search(r"夏天|夏季|暑假|夏日", text):
        vec["time"]["season"] = "summer"
        explicit.add("time.season")
    elif re.search(r"秋天|秋季|金秋|秋日", text):
        vec["time"]["season"] = "autumn"
        explicit.add("time.season")
    elif re.search(r"春天|春季|春日", text):
        vec["time"]["season"] = "spring"
        explicit.add("time.season")

    # ---------- 气候诉求（结构化，供目的地级推荐） ----------
    if re.search(r"暖和|温暖|避寒|晒太阳", text):
        vec["geography"]["climate_wish"] = {"min_day_temp_c": 15}
        _mark_inferred(vec, "geography.climate_wish")
        explicit.add("geography.climate_wish")
    elif re.search(r"避暑|凉快|凉爽|怕热", text):
        vec["geography"]["climate_wish"] = {"max_day_temp_c": 28}
        _mark_inferred(vec, "geography.climate_wish")
        explicit.add("geography.climate_wish")

    # ---------- 预算 ----------
    m = re.search(r"(人均|预算|每人)\s*(?:约|大概|大约|左右|不超过|以内|控制在)?\s*([0-9]+(?:\.[0-9]+)?)\s*(万|w|W|千|k|K|元|块)?", text)
    if m:
        amount = float(m.group(2))
        unit = m.group(3)
        if unit in ("万", "w", "W"):
            amount *= 10000
        elif unit in ("千", "k", "K"):
            amount *= 1000
        elif unit is None and amount < 100:
            amount *= 1000  # "人均8" 视为 8 千（口语省略单位的既有约定）
        # sanity band：人均 CNY 合理区间 200–100000。"人均8元"=8 元之类
        # 超界金额不采用、不外推猜测——per_person_cny 保持 None，由 uncertainty 追问
        if 200 <= amount <= 100000:
            vec["budget"]["per_person_cny"] = amount
            explicit.add("budget.per_person_cny")
            # 金额 → 消费档位（档位是推导值，进 inferred）
            tier = 2 if amount <= 3000 else (3 if amount <= 8000 else 4)
            vec["budget"]["tier_preference"] = tier
            _mark_inferred(vec, "budget.tier_preference")
            if re.search(r"不超过|以内|封顶|最多", text):
                vec["budget"]["hard_cap"] = True
                explicit.add("budget.hard_cap")
    if re.search(r"穷游|背包客|能省则省", text):
        vec["budget"]["tier_preference"] = min(vec["budget"]["tier_preference"], 2)
        _mark_inferred(vec, "budget.tier_preference")
        explicit.add("budget.tier_preference")
    elif re.search(r"省钱|性价比", text):
        vec["budget"]["tier_preference"] = min(vec["budget"]["tier_preference"], 2)
        _mark_inferred(vec, "budget.tier_preference")
    if re.search(r"奢华|豪华|奢侈|高端|贵妇|不差钱", text):
        vec["budget"]["tier_preference"] = 4
        _mark_inferred(vec, "budget.tier_preference")
        explicit.add("budget.tier_preference")

    # ---------- 步调 ----------
    if re.search(r"特种兵|暴走|高强度|拉练|打卡式|一天跑", text):
        vec["pace"]["atoms_per_day"] = 5
        vec["pace"]["early_start_ok"] = True
        vec["pace"]["rest_afternoon"] = False
        explicit.add("pace")
    elif re.search(r"轻松|不累|太累|悠闲|慢慢来|慢慢逛|佛系|躺平|松弛|慢慢", text):
        vec["pace"]["atoms_per_day"] = 2 if "elderly_parents" in comp else 3
        vec["pace"]["rest_afternoon"] = True
        vec["pace"]["early_start_ok"] = False
        explicit.add("pace")
    if re.search(r"不早起|睡到自然醒|不想早起", text):
        vec["pace"]["early_start_ok"] = False
        explicit.add("pace")
    if "pace" not in explicit and "elderly_parents" in comp:
        # 带老人默认收慢（spec §3：老人 2-3 个原子/天）
        vec["pace"]["atoms_per_day"] = min(vec["pace"]["atoms_per_day"], 3)
        _mark_inferred(vec, "pace.atoms_per_day")

    # ---------- 情绪目标（枚举同供给 schema §4.2） ----------
    emotion_rules = [
        (r"拍照|出片|拍大片|机位|写真", "photo"),
        (r"美食|吃货|觅食|吃遍|好吃|小吃|吃吃喝喝", "foodie"),
        (r"治愈|放松|解压|放空|疗愈", "healing"),
        (r"小众|冷门|人少|避开人群|私藏|秘境", "hidden_gem"),
        (r"必打卡|打卡|地标|经典景点", "iconic"),
        (r"夜景|夜色|灯光秀", "night_view"),
        (r"浪漫|约会|蜜月", "romantic"),
        (r"当地人|烟火气|市井|本地生活|在地", "lively_local"),
        (r"文化|历史|人文|古迹|博物馆|寺庙|深度体验|深度", "cultural_deep"),
        (r"亲子|乐园|遛娃", "family_fun"),
        (r"怀旧|复古|老街", "nostalgic"),
        (r"冒险|刺激|极限", "adventure"),
        (r"避世|安静|清净|人少安静", "quiet_escape"),
        (r"奢华|贵妇|奢享", "luxury"),
    ]
    for pattern, tag in emotion_rules:
        if re.search(pattern, text):
            _add_unique(vec["emotion_goals"], tag)
    if vec["emotion_goals"]:
        explicit.add("emotion_goals")

    # 购物在枚举里没有独立 emotion tag → 走 explicit_interests（spec 任务约定）
    if re.search(r"购物|买买买|血拼|免税|奥莱|奥特莱斯", text):
        _add_unique(vec["explicit_interests"], "shopping")
        explicit.add("explicit_interests")
    if re.search(r"温泉|泡汤|泡个汤", text):
        _add_unique(vec["explicit_interests"], "onsen")
        explicit.add("explicit_interests")

    # ---------- 硬约束 / 显式排除 ----------
    if re.search(r"不坐红眼|不要红眼|拒绝红眼|红眼航班|不赶红眼", text):
        _add_unique(vec["hard_constraints"], "no_red_eye_flight")
        explicit.add("hard_constraints")
    if re.search(r"不(?:要|想|去)?爬山|不(?:要|想)?徒步|不登山|不(?:要|想)爬", text) or \
       re.search(r"不累|别太累|不要太累|不想太累|怕累|不能太累", text):
        _add_unique(vec["explicit_exclusions"], "strenuous_hiking")
        explicit.add("explicit_exclusions")

    # ---------- 飞行时长上限 ----------
    m = re.search(r"([0-9]+)\s*(?:个)?小时(?:以内|之内|内)?(?:的)?(?:飞行|航程|航班|飞机)", text)
    if m:
        vec["geography"]["max_flight_hours"] = float(m.group(1))
        explicit.add("geography.max_flight_hours")

    # ---------- 目的地 ----------
    found = [DESTINATIONS[name] for name in DESTINATIONS if name in text]
    if found:
        vec["geography"]["destination_specified"] = found[0] if len(found) == 1 else found
        explicit.add("geography.destination_specified")

    # ---------- 置信度（简单可解释的启发式） ----------
    confidence = 0.35
    confidence += 0.12 if "geography.destination_specified" in explicit else 0.0
    confidence += 0.08 if "time.duration_days" in explicit else 0.0
    confidence += 0.08 if vec["time"]["departure_date_range"] is not None else 0.0
    confidence += 0.08 if "party.composition" in explicit else 0.0
    confidence += 0.06 if "time.season" in explicit else 0.0
    confidence += 0.06 if ("budget.per_person_cny" in explicit or "budget.tier_preference" in explicit) else 0.0
    confidence += 0.05 if "emotion_goals" in explicit else 0.0
    confidence += 0.05 if "pace" in explicit else 0.0
    confidence += 0.03 if "geography.climate_wish" in explicit else 0.0
    confidence += 0.03 if ("hard_constraints" in explicit or "explicit_exclusions" in explicit) else 0.0
    vec["confidence"] = round(max(0.1, min(0.95, confidence)), 2)

    # ---------- 不确定项（草稿先行的落点：≤3 问，每问 ≤4 个可点选项） ----------
    uncertainty: list[dict] = []
    if vec["geography"]["destination_specified"] is None:
        if vec["geography"]["climate_wish"] and "min_day_temp_c" in vec["geography"]["climate_wish"]:
            options = ["大阪", "曼谷", "按暖和帮我推荐", "还没定"]
            question = "有想去的城市吗？还是按「暖和」帮你推荐？"
        else:
            options = ["大阪", "曼谷", "京都", "帮我推荐"]
            question = "有想去的城市吗？还是按偏好帮你推荐？"
        uncertainty.append({
            "field": "geography.destination_specified",
            "question": question,
            "options": options,
        })
    if vec["time"]["departure_date_range"] is None:
        uncertainty.append({
            "field": "time.departure_date_range",
            "question": "大概什么时候出发？",
            "options": ["最近两周内", "1-2 个月内", "3 个月以后", "还没定"],
        })
    if vec["budget"]["per_person_cny"] is None:
        uncertainty.append({
            "field": "budget.per_person_cny",
            "question": "人均预算大概多少？",
            "options": ["3k 内", "5k-8k", "8k-1.5w", "1.5w+"],
        })
    vec["uncertainty"] = uncertainty[:3]  # spec §1：一次 ≤ 3 问

    return vec
