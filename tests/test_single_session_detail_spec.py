"""Offline contract checks for the destination detail generation instructions."""
import json
import re
import unittest

from packages.travel_agent.single_session_agent import DESTINATION_DETAIL_SPEC, SYSTEM


class DestinationDetailSpecTests(unittest.TestCase):
    def test_structured_example_is_json_and_covers_render_contract(self):
        blocks = re.findall(r'```destination-detail-v1\n(.*?)\n```', DESTINATION_DETAIL_SPEC, re.S)
        self.assertEqual(len(blocks), 1)
        detail = json.loads(blocks[0])
        self.assertEqual(detail['version'], 1)
        day = detail['days'][0]
        self.assertEqual(set(day), {'id', 'title', 'date', 'city', 'country', 'summary', 'stops', 'segments',
                                    'timeline', 'attractions', 'hotels', 'dining'})
        self.assertEqual(set(day['segments'][0]), {'from', 'to', 'distance_km',
                         'duration_minutes', 'status', 'basis', 'price_or_timetable'})
        self.assertEqual(set(day['timeline'][0]), {'time', 'kind', 'action', 'place', 'duration_minutes'})
        self.assertEqual(set(day['attractions'][0]), {'name', 'city', 'country', 'address', 'see', 'route', 'stay',
            'best_time', 'pitfall', 'tips', 'season', 'other_seasons', 'images',
            'missing', 'stops', 'segments'})
        for field in ('city', 'country'):
            self.assertIsNone(day[field])
        for place in day['stops'] + day['attractions']:
            for field in ('city', 'country', 'address'):
                self.assertIsNone(place[field])
        self.assertIsNone(day['stops'][0]['coordinates'])
        self.assertIsNone(day['segments'][0]['distance_km'])
        self.assertIsNone(day['timeline'][0]['time'])
        self.assertIn(DESTINATION_DETAIL_SPEC, SYSTEM)

    def test_complete_density_and_bounded_research_requirements(self):
        requirements = [
            '早餐/午餐/晚餐', '每段交通', '当晚酒店条目', '带活动时长',
            '距离千米＋时长分钟', '价格量级或官方时刻至少其一',
            '2–3 家具体酒店名', '同档备选', '档次标注', '至少 1 个具体餐饮',
            '入口→内部动线→出口', '建议停留时长', '最佳时段＋理由', '一个具体坑',
            '2–3 条具体玩法提示', '站点—距离—站点', '本季看什么', '其他季节一句话',
            '门票/区间车/自驾/主要项目', '至少 2 条', '说法出处类型与实际口径日期',
            '日出/日落', '计算值/查表值', '时区与可复现依据', '分项量级＋总额区间',
            '已核项与估算项分开标', 'S1 证据目标不得低于 12 条量级',
            '先官方/地图', '近期一手游记', '超过一季', '有限轮次与时间预算',
            '连续两轮无新证据', '未达到 12 条的差额', '不绕登录/App/挑战墙',
        ]
        for requirement in requirements:
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, DESTINATION_DETAIL_SPEC)

    def test_single_fact_authority_and_existing_price_gate_remain_explicit(self):
        for requirement in [
            '不在 block 外重复日程事实', '每日正文唯一事实来源',
            'summary 只讲取舍，不重复数字事实', 'prose_day_replaced',
            '不是自然语言语义核验通过', '编辑时同步受影响的结构字段',
            'from/to 字段引用本日 stop id', '不在 action 重写数字',
            '以 segment duration_minutes 为准', 'overview 统一维护',
            '{{price:E1}}', '不得在结构块中手写新数字价格', '以「估算」绕过绑定',
            'budget_totals', '不能拿零代替未知', '无证据填 null',
            '房价查不到实时值就不标价', '单源未交叉确认',
        ]:
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, DESTINATION_DETAIL_SPEC)


if __name__ == '__main__':
    unittest.main()
