"""OSM opening_hours 标签的务实解析器（BACKLOG #2）。

覆盖目标：data/raw/overpass_*.json 中实际出现格式的 ≥80%（见 audit 覆盖率报告）。
已支持的语法子集（按真实数据调查选定）：
- "24/7"
- 纯时间段："10:00-22:00"，跨夜 "18:00-01:00"，终点 "24:00"
- 星期选择器："Mo-Fr 09:00-17:00"、"Tu-Su"、"Mo,We,Fr"、"Sat-Sun"（三字母容错）
- PH（公共假期）选择器："Mo-Su,PH 10:00-20:00"、"Su,PH off"
- 同规则多时段（逗号）："09:00-12:00,15:30-19:30"
- 规则内嵌套选择器："Mo-Fr 10:00-21:00, Sa,Su 09:00-21:00"
- off/closed 闭店规则："Mo off"、"PH off"、"Tu-Su,PH 10:00-17:00; Mo closed"
- 月-日区间："Dec 28-Jan 05 off"、"Jan 1-Mar 31 06:00-18:00"（跨年环绕）、
  单日 "Jan 01 off"
- 分号多规则组合

明确不支持 → status "unknown"（绝不猜测）：
- sunrise-sunset、nth weekday（Su[3]）、周数、"不定休"等自然语言、
  裸时刻（"12:00"）、"每月第 N 日"（"8 day 18day off"）等。

PH 语义：没有假期日历时绝不臆断。evaluate(holidays=...) 可传入
set[datetime.date] 假期表得到精确结果；缺省时，若结果取决于"今天是否
是公共假期"（PH 规则会翻转结论），返回 unknown 并注明 ph_uncertain。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta

# ---- 星期与月份表 ----
_WD = {"mo": 0, "tu": 1, "we": 2, "th": 3, "fr": 4, "sa": 5, "su": 6,
       "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_MON = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

_DAY_NAMES = r"mo|tu|we|th|fr|sa|su|mon|tue|wed|thu|fri|sat|sun"
_MON_NAMES = r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"

_RE_SPAN = re.compile(
    r"^(\d{1,2})[:.](\d{2})\s*-\s*(\d{1,2})[:.](\d{2})$")
_RE_FIRST_TIME = re.compile(r"\d{1,2}[:.]\d{2}")
_RE_QUOTED = re.compile(r'"[^"]*"')
# 选择器片段扫描器（在选择器头部上逐段匹配）
_RE_MD_RANGE = re.compile(
    rf"({_MON_NAMES})\s+(\d{{1,2}})\s*-\s*({_MON_NAMES})\s+(\d{{1,2}})", re.I)
_RE_MD_RANGE_SAME_MON = re.compile(
    rf"({_MON_NAMES})\s+(\d{{1,2}})\s*-\s*(\d{{1,2}})(?!\d)", re.I)  # "Jan 01-03"
_RE_MD_SINGLE = re.compile(rf"({_MON_NAMES})\s+(\d{{1,2}})(?!\s*[-\d])", re.I)
_RE_DAY_RANGE = re.compile(rf"({_DAY_NAMES})\s*-\s*({_DAY_NAMES})(?!\w)", re.I)
_RE_DAY = re.compile(rf"({_DAY_NAMES})(?!\w)", re.I)
_RE_PH = re.compile(r"PH(?!\w)", re.I)
_RE_SEP = re.compile(r"[\s,]+")

MIN_PER_DAY = 24 * 60


@dataclass
class Rule:
    """一条规则：选择器 + 时段（或 off）。"""
    weekdays: set[int] | None = None            # None = 任意星期
    ph_include: bool = False                    # PH 在选择器内（含）
    md_ranges: list[tuple[tuple[int, int], tuple[int, int]]] = field(default_factory=list)
    spans: list[tuple[int, int]] = field(default_factory=list)  # 分钟；end 可 >1440（跨夜）
    off: bool = False

    def has_selector(self) -> bool:
        return bool(self.weekdays) or self.ph_include or bool(self.md_ranges)

    def complete(self) -> bool:
        return self.off or bool(self.spans)


@dataclass
class OpeningHours:
    raw: str
    is_24_7: bool = False
    rules: list[Rule] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)  # 解析失败的规则原文

    @property
    def fully_parsed(self) -> bool:
        return self.is_24_7 or (bool(self.rules) and not self.unsupported)


def _parse_hm(h: str, m: str, max_h: int = 24) -> int | None:
    hi, mi = int(h), int(m)
    if mi >= 60 or hi > max_h or (hi == 24 and mi != 0 and max_h == 24):
        return None
    return hi * 60 + mi


def _md_in_ranges(md: tuple[int, int],
                  ranges: list[tuple[tuple[int, int], tuple[int, int]]]) -> bool:
    for lo, hi in ranges:
        if lo <= hi:
            if lo <= md <= hi:
                return True
        else:  # 跨年环绕，如 Dec 28-Jan 05
            if md >= lo or md <= hi:
                return True
    return False


def _parse_selector_head(head: str, rule: Rule) -> bool:
    """扫描选择器头部（可能含空格/逗号分隔的多个片段）。全部识别返回 True。"""
    pos, n = 0, len(head)
    matched_any = False
    while pos < n:
        sep = _RE_SEP.match(head, pos)
        if sep:
            pos = sep.end()
        if pos >= n:
            break
        m = _RE_MD_RANGE.match(head, pos)
        if m:
            lo = (_MON[m.group(1).lower()], int(m.group(2)))
            hi = (_MON[m.group(3).lower()], int(m.group(4)))
            rule.md_ranges.append((lo, hi))
            pos, matched_any = m.end(), True
            continue
        m = _RE_MD_RANGE_SAME_MON.match(head, pos)
        if m:
            lo = (_MON[m.group(1).lower()], int(m.group(2)))
            hi = (_MON[m.group(1).lower()], int(m.group(3)))
            rule.md_ranges.append((lo, hi))
            pos, matched_any = m.end(), True
            continue
        m = _RE_DAY_RANGE.match(head, pos)
        if m:
            a, b = _WD[m.group(1).lower()], _WD[m.group(2).lower()]
            days = (set(range(a, b + 1)) if a <= b
                    else set(range(a, 7)) | set(range(0, b + 1)))
            rule.weekdays = (rule.weekdays or set()) | days
            pos, matched_any = m.end(), True
            continue
        m = _RE_MD_SINGLE.match(head, pos)
        if m:
            md = (_MON[m.group(1).lower()], int(m.group(2)))
            rule.md_ranges.append((md, md))
            pos, matched_any = m.end(), True
            continue
        m = _RE_DAY.match(head, pos)
        if m:
            rule.weekdays = (rule.weekdays or set()) | {_WD[m.group(1).lower()]}
            pos, matched_any = m.end(), True
            continue
        m = _RE_PH.match(head, pos)
        if m:
            rule.ph_include = True
            pos, matched_any = m.end(), True
            continue
        return False  # 无法识别的片段 → 整条规则不支持
    return matched_any


def _apply_tail(rule: Rule, tail: str) -> bool:
    """把段尾（时段 / off / closed / open）应用到规则。"""
    low = tail.strip().lower()
    if not low:
        return True
    if low in ("off", "closed"):
        rule.off = True
        return True
    if low == "open":
        rule.spans.append((0, MIN_PER_DAY))
        return True
    m = _RE_SPAN.match(low)
    if m:
        t1 = _parse_hm(m.group(1), m.group(2))
        t2 = _parse_hm(m.group(3), m.group(4), max_h=48)  # "11:00-25:30" = 次日 01:30
        if t1 is None or t2 is None or t1 == t2:
            return False
        rule.spans.append((t1, t2 if t2 > t1 else t2 + MIN_PER_DAY))
        return True
    return False


def _parse_rule(text: str) -> list[Rule] | None:
    """解析单条 ';' 规则为一个或多个选择器组。失败返回 None。

    逗号把规则切段：纯时段段并入当前组；含选择器的段开启新组
    （前面暂存的纯选择器段合并进来）——覆盖 "Mo-Fr 10:00-21:00, Sa,Su 09:00-21:00"
    与 "PH,Sa,Su 10:00-21:00" 两种真实形态。
    """
    groups: list[Rule] = []
    pending = Rule()  # 正在积累的选择器组
    saw_anything = False

    for seg in text.split(","):
        seg = seg.strip()
        if not seg:
            continue
        saw_anything = True
        low = seg.lower()
        # 1) 纯关键字段
        if low in ("off", "closed", "open"):
            if not _apply_tail(pending, low):
                return None
            groups.append(pending)
            pending = Rule()
            continue
        # 2) 纯时段段
        if _RE_SPAN.match(low):
            target = pending if (pending.has_selector() or not groups) else groups[-1]
            if not _apply_tail(target, low):
                return None
            if target is pending and not pending.has_selector():
                pass  # 无选择器的开头时段（"10:00-22:00"），留在 pending 继续积累
            continue
        # 3) 选择器段（可能自带段尾）
        mt = _RE_FIRST_TIME.search(seg)
        if mt:
            head, tail = seg[:mt.start()], seg[mt.start():]
        else:
            kw = re.search(r"(?i)\b(off|closed|open)\s*$", seg)
            if kw and kw.group(1).lower() in ("off", "closed", "open"):
                head, tail = seg[:kw.start()], kw.group(1)
            else:
                head, tail = seg, ""
        if pending.complete():
            groups.append(pending)
            pending = Rule()
        if head.strip():
            if not _parse_selector_head(head, pending):
                return None
        if tail.strip():
            if not _apply_tail(pending, tail):
                return None
    if not saw_anything:
        return None
    if pending.has_selector() or pending.complete():
        groups.append(pending)
    if not groups:
        return None
    # 悬挂的纯选择器组（无时段无 off）→ 语义不全，整条不支持
    for g in groups:
        if not g.complete():
            return None
    return groups


def parse(value: str) -> OpeningHours:
    """解析 opening_hours 标签值。永不抛异常；失败部分记入 .unsupported。"""
    raw = (value or "").strip()
    oh = OpeningHours(raw=raw)
    if not raw:
        oh.unsupported.append("(empty)")
        return oh
    if raw == "24/7":
        oh.is_24_7 = True
        return oh
    # 去掉引号注释（"不定休" 等）
    stripped = _RE_QUOTED.sub("", raw).strip().rstrip(";").strip()
    if not stripped:
        oh.unsupported.append(raw)
        return oh
    for part in stripped.split(";"):
        part = part.strip()
        if not part:
            continue
        rules = _parse_rule(part)
        if rules is None:
            oh.unsupported.append(part)
        else:
            oh.rules.extend(rules)
    return oh


def _rule_matches(rule: Rule, d: date, is_holiday: bool | None) -> bool:
    """规则选择器是否覆盖日期 d（is_holiday=None 时 PH 条件按不匹配处理）。"""
    if rule.md_ranges and not _md_in_ranges((d.month, d.day), rule.md_ranges):
        return False
    if rule.weekdays is not None and d.weekday() in rule.weekdays:
        return True
    if rule.weekdays is None and not rule.ph_include:
        return True
    if rule.ph_include and is_holiday is True:
        return True
    return False


def _ph_could_flip(oh: OpeningHours, d: date) -> bool:
    """无假期日历时，结论是否取决于"今天是否公共假期"。"""
    for r in oh.rules:
        if r.md_ranges and not _md_in_ranges((d.month, d.day), r.md_ranges):
            continue
        wd_match = r.weekdays is None or d.weekday() in r.weekdays
        if r.ph_include and not wd_match:
            # 仅当今天是假期才生效的规则（开或关）→ 结论可能被翻转
            return True
    return False


def evaluate(value_or_oh, when: datetime,
             holidays: set[date] | None = None) -> dict:
    """评估 when 时刻的营业状态。

    返回 {"status": "open"|"closed"|"unknown", "detail": str}。
    holidays：可选公共假期日期集合；缺省时若结论依赖 PH 语义 → unknown。
    铁律：解析不了 / 选择器不覆盖 / PH 不确定 → 一律 unknown，绝不猜测。
    """
    oh = value_or_oh if isinstance(value_or_oh, OpeningHours) else parse(value_or_oh)
    if oh.is_24_7:
        return {"status": "open", "detail": "24/7"}
    if oh.unsupported:
        return {"status": "unknown",
                "detail": f"unparseable rules: {'; '.join(oh.unsupported)[:120]}"}
    if not oh.rules:
        return {"status": "unknown", "detail": "no rules parsed"}

    d = when.date()
    is_holiday = (d in holidays) if holidays is not None else None

    if is_holiday is None and _ph_could_flip(oh, d):
        return {"status": "unknown",
                "detail": "ph_uncertain: depends on public-holiday semantics "
                          "(no holiday calendar supplied)"}

    t = when.hour * 60 + when.minute
    prev = d - timedelta(days=1)
    prev_holiday = (prev in holidays) if holidays is not None else None

    # 显式闭店规则优先（"Mo off" / "Su,PH off" 形态）
    for r in oh.rules:
        if r.off and _rule_matches(r, d, is_holiday):
            return {"status": "closed", "detail": f"off rule matched for {d.isoformat()}"}

    # 当日时段 + 前一日跨夜溢出（如 Fr 18:00-01:00 覆盖周六 00:30）
    matched_day = False
    for day0, t_off, ih in ((d, 0, is_holiday), (prev, MIN_PER_DAY, prev_holiday)):
        for r in oh.rules:
            if r.off:
                continue
            if _rule_matches(r, day0, ih):
                if r.spans:
                    matched_day = True
                for lo, hi in r.spans:
                    if lo <= t + t_off < hi:
                        hi_clk = hi % MIN_PER_DAY
                        return {"status": "open",
                                "detail": f"within span {lo // 60:02d}:{lo % 60:02d}-"
                                          f"{hi_clk // 60:02d}:{hi_clk % 60:02d}"}
    if matched_day:
        return {"status": "closed", "detail": "outside all open spans for this day"}
    return {"status": "unknown", "detail": f"no selector covers {d.strftime('%A')}"}


def coverage_report(values: list[str]) -> dict:
    """解析覆盖率报告：对一组真实标签值统计 fully_parsed 比例与失败样本。"""
    total = len(values)
    ok, is247, fails = 0, 0, []
    for v in values:
        oh = parse(v)
        if oh.is_24_7:
            ok += 1
            is247 += 1
        elif oh.rules and not oh.unsupported:
            ok += 1
        else:
            fails.append(v)
    return {
        "total": total,
        "parsed": ok,
        "parse_coverage": round(ok / total, 4) if total else 0.0,
        "of_which_24_7": is247,
        "unparsed": len(fails),
        "unparsed_samples": fails[:20],
    }
