import unittest

from packages.travel_agent.single_session_legacy_presentation import extract_legacy_media


class LegacyMediaExtractionTests(unittest.TestCase):
    def test_explicit_cards_and_timeline_places(self):
        prose = '''# 出行手册
## 第一天 · 游园
| 时间 | 活动 | 地点 |
| --- | --- | --- |
| 09:00 | 看花 | 公园甲 |
| 10:30 | 看展 | 博物馆乙 |
### 公园甲
- **看什么：** 花园
- 停多久：一小时
### 博物馆乙
坑在哪：入口另见正文
## 第二天 · 山水
### 山庄丙
怎么走：沿正门进
## 预算
### 不应抓取
看什么：预算分类
'''
        result = extract_legacy_media(prose)
        self.assertEqual(['第一天 · 游园', '第二天 · 山水'], [d['title'] for d in result['days']])
        self.assertEqual(['公园甲', '博物馆乙'], [s['name'] for s in result['days'][0]['stops']])
        self.assertEqual(['公园甲', '博物馆乙'], [s['name'] for s in result['days'][0]['attractions']])
        self.assertEqual([{'name': '山庄丙'}], result['days'][1]['attractions'])
        self.assertEqual([], result['days'][1]['stops'])
        self.assertEqual(result, extract_legacy_media(prose))

    def test_no_inferred_places_from_prose_action_or_heading(self):
        result = extract_legacy_media('''## Day 1: Walk
上午去西湖。
- 09:00 游西湖
- 10:00 逛公园 | 公园甲
| 时间 | 活动 | 地点 |
| 11:00 | 游博物馆 | |
### 午餐安排
推荐湖边用餐
''')
        self.assertEqual([], result['days'][0]['stops'])
        self.assertEqual([], result['days'][0]['attractions'])

    def test_generic_unnumbered_document_returns_none(self):
        self.assertIsNone(extract_legacy_media('# 手册\n## 游览安排\n### 公园\n看什么：鲜花'))
        self.assertIsNone(extract_legacy_media(None))

    def test_fenced_examples_ignored_and_order_preserved(self):
        result = extract_legacy_media('''```markdown
## 第一天
### 假景点
看什么：代码示例
```
## D2 · 实际行程
### 乙园
停多久：一小时
## D3 · 次日
### 甲园
坑在哪：末班车
''')
        self.assertEqual(['D2 · 实际行程', 'D3 · 次日'], [d['title'] for d in result['days']])
        self.assertEqual(['乙园', '甲园'], [d['attractions'][0]['name'] for d in result['days']])
        self.assertEqual(['legacy-day-1', 'legacy-day-2'], [d['id'] for d in result['days']])

    def test_repeat_stop_retains_explicit_return_visit(self):
        result = extract_legacy_media('''## 第一天
| 时间 | 安排 | 站点 |
| 09:00 | 集合 | 湖园 |
| 12:00 | 午餐 | 餐厅 |
| 14:00 | 回访 | 湖园 |
''')
        stops = result['days'][0]['stops']
        self.assertEqual(['湖园', '餐厅', '湖园'], [s['name'] for s in stops])
        self.assertEqual(3, len({s['id'] for s in stops}))


if __name__ == '__main__':
    unittest.main()
