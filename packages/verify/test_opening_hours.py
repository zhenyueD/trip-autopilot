"""opening_hours 解析器单元测试（stdlib unittest，~15 例）。

运行：python3 -m unittest packages.verify.test_opening_hours -v
用例取自 data/raw/overpass_*.json 真实出现格式 + 边界/畸形输入。
锚点日期：2026-09-14 是周一，09-15 周二，09-19 周六，09-20 周日。
"""
from __future__ import annotations

import unittest
from datetime import datetime, date

from packages.verify.opening_hours import parse, evaluate, coverage_report

MON = datetime(2026, 9, 14, 10, 0)   # 周一 10:00
TUE = datetime(2026, 9, 15, 15, 0)   # 周二 15:00
SAT = datetime(2026, 9, 19, 11, 0)   # 周六 11:00
SUN = datetime(2026, 9, 20, 11, 0)   # 周日 11:00


class TestOpeningHoursParser(unittest.TestCase):

    def st(self, value, when, holidays=None):
        return evaluate(value, when, holidays=holidays)["status"]

    # 1. 工作日时段
    def test_weekday_range_open(self):
        self.assertEqual(self.st("Mo-Fr 09:00-17:00", MON), "open")

    # 2. 工作日时段外 → closed
    def test_weekday_range_closed_outside_hours(self):
        self.assertEqual(self.st("Mo-Fr 09:00-17:00",
                                 datetime(2026, 9, 14, 20, 0)), "closed")

    # 3. 周末无选择器覆盖 → unknown（绝不猜 closed）
    def test_weekday_range_weekend_unknown(self):
        self.assertEqual(self.st("Mo-Fr 09:00-17:00", SUN), "unknown")

    # 4. 24/7
    def test_24_7(self):
        self.assertEqual(self.st("24/7", datetime(2026, 9, 20, 3, 30)), "open")

    # 5. 每周七天
    def test_mo_su(self):
        self.assertEqual(self.st("Mo-Su 10:00-22:00", SUN), "open")

    # 6. 休日规则（off day）
    def test_off_day(self):
        self.assertEqual(self.st("Mo-Su 10:00-20:00; Tu off", TUE), "closed")
        self.assertEqual(self.st("Mo-Su 10:00-20:00; Tu off", MON), "open")

    # 7. closed 关键字等价 off
    def test_closed_keyword(self):
        self.assertEqual(self.st("Tu-Su 10:00-18:00; Mo closed", MON), "closed")
        self.assertEqual(self.st("Tu-Su 10:00-18:00; Mo closed", TUE), "open")

    # 8. 逗号多时段（午间休市）
    def test_multi_span_lunch_break(self):
        v = "Mo-Fr 09:00-12:00,15:30-19:30"
        self.assertEqual(self.st(v, MON), "open")
        self.assertEqual(self.st(v, datetime(2026, 9, 14, 13, 0)), "closed")

    # 9. 规则内嵌套选择器（周末时段不同）
    def test_nested_selectors_in_rule(self):
        v = "Mo-Fr 10:00-21:00, Sa,Su 09:00-21:00"
        self.assertEqual(self.st(v, datetime(2026, 9, 14, 9, 30)), "closed")
        self.assertEqual(self.st(v, datetime(2026, 9, 19, 9, 30)), "open")

    # 10. 跨夜时段：周五 18:00-01:00 覆盖周六凌晨
    def test_overnight_span(self):
        v = "Fr 18:00-01:00"
        self.assertEqual(self.st(v, datetime(2026, 9, 19, 0, 30)), "open")
        self.assertEqual(self.st(v, datetime(2026, 9, 19, 2, 0)), "closed")

    # 11. 终点 24:00 与 >24h 终点（25:30 = 次日 01:30）
    def test_end_24_and_beyond(self):
        self.assertEqual(self.st("Mo-Su 09:00-24:00",
                                 datetime(2026, 9, 15, 23, 30)), "open")
        self.assertEqual(self.st("Mo-Su 11:00-25:30",
                                 datetime(2026, 9, 16, 1, 0)), "open")

    # 12. PH 语义：无假期日历且结论依赖 PH → unknown；给日历则精确
    def test_ph_uncertain_without_calendar(self):
        v = "Mo-Th 11:00-17:00; Sa 14:00-17:00; PH Fr Su off"
        self.assertEqual(self.st(v, TUE), "unknown")  # 周二可能是假期
        # 提供假期表（周二非假期）→ open；周二若是假期 → closed
        self.assertEqual(self.st(v, TUE, holidays=set()), "open")
        self.assertEqual(self.st(v, TUE, holidays={date(2026, 9, 15)}), "closed")

    # 13. PH 含在选择器内但 Mo-Su 已全覆盖 → 不受 PH 不确定性影响
    def test_ph_redundant_with_mo_su(self):
        self.assertEqual(self.st("Mo-Su,PH 10:00-20:00", TUE), "open")

    # 14. 月-日区间闭馆（跨年环绕）
    def test_monthday_range_off(self):
        v = "Tu-Su,PH 10:00-18:00; Jan 01-03,Dec 29-31 off"
        self.assertEqual(self.st(v, datetime(2026, 1, 2, 12, 0)), "closed")
        self.assertEqual(self.st(v, datetime(2026, 12, 30, 12, 0)), "closed")
        self.assertEqual(self.st(v, TUE), "open")

    # 15. 畸形/不支持输入 → unknown，绝不猜测
    def test_malformed_unknown(self):
        for bad in ("sunrise-sunset", "12:00", "none",
                    "08:00+", "Apr-Nov: Su[3] 10:00-12:00",
                    "10:00-20:00 \"不定休\"  XYZ", "", "Mo-Su 25:00-26:00"):
            self.assertEqual(self.st(bad, MON), "unknown", msg=bad)

    # 15b. 整值 "closed" 是有效数据（永久闭店），不是畸形输入
    def test_whole_value_closed(self):
        self.assertEqual(self.st("closed", MON), "closed")

    # 16. 三字母星期容错 + 带空格逗号
    def test_three_letter_days_and_spaces(self):
        self.assertEqual(self.st("Sat-Sun 09:30-16:30", SAT), "open")
        self.assertEqual(self.st("Mo, We, Fr 17:00-20:00",
                                 datetime(2026, 9, 16, 18, 0)), "open")

    # 17. 解析结构健全性：fully_parsed 与 unsupported 记录
    def test_parse_structure(self):
        self.assertTrue(parse("24/7").is_24_7)
        self.assertTrue(parse("Mo-Fr 09:00-17:00").fully_parsed)
        bad = parse("Mo-Fr 09:00-17:00; Su[1] off")
        self.assertFalse(bad.fully_parsed)
        self.assertIn("Su[1] off", bad.unsupported[0])

    # 18. 覆盖率报告结构
    def test_coverage_report(self):
        rep = coverage_report(["24/7", "Mo-Su 10:00-22:00", "sunrise-sunset"])
        self.assertEqual(rep["total"], 3)
        self.assertEqual(rep["parsed"], 2)
        self.assertAlmostEqual(rep["parse_coverage"], round(2 / 3, 4))
        self.assertEqual(rep["unparsed_samples"], ["sunrise-sunset"])


if __name__ == "__main__":
    unittest.main()
