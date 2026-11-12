"""Offline rendering fixture: extends DEST-DEPTH's synthetic east-gate asset."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from packages.travel_agent.single_session_document import DocumentStore, render_snapshot


MARKDOWN = '''# 离线夹具手册（非真实旅行建议）
## 第一天 · 测试路线
| 时刻 | 事项 | 地点 |
| --- | --- | --- |
| 07:00 | 起床 | 测试酒店 |
| 09:00 | 从测试酒店到测试景点，乘车20分钟 | 测试景点 |
| 18:00 | 返程耗时待确认，回酒店 | 测试酒店 |
### 测试景点
- 看什么：东门建筑，未交叉确认。
- 怎么走：东门→庭院→东门。
- 停多久：建议1小时。
- 什么时候最好：上午，光线条件待确认。
- 坑在哪：入口待现场确认。
## 第二天 · 休息
- 08:00 起床 | 测试酒店
- 19:00 回酒店 | 测试酒店
'''


def fixture(directory):
    store = DocumentStore(directory)
    # Same shape and synthetic image/coordinate values as DEST-DEPTH fixture.
    for name, point, lat, lon, image in [
        ('测试景点', '东门', 31.25, 121.5, 'https://example.test/east-gate.jpg'),
        ('测试酒店', '入口', 31.26, 121.51, None),
    ]:
        url = 'https://example.test/map/' + str(lat)
        quote = f'{name} {point}; coordinates {lat}, {lon} WGS84; photo {image}'
        store.register_source(name, url, quote, '2026-11-05')
        asset = dict(destination_asset=dict(place=name, point=point,
            images=[dict(url=image, source_url=url)] if image else [],
            coordinates=dict(latitude=lat, longitude=lon, crs='WGS84', source_url=url), missing=[]))
        store.record_evidence(name, quote, json.dumps(asset, ensure_ascii=False), 'other')
    return dict(markdown=MARKDOWN, receipt=dict(changes='初稿', judgment='离线夹具', alternatives='无', next_action='人工验收')), store._read_json('ledger.json')


class DestinationVisualTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.snapshot, self.ledger = fixture(temporary.name)

    def page(self):
        return render_snapshot(self.snapshot, self.ledger)['html']

    def asset(self, update, eid='E1'):
        value = json.loads(self.ledger['evidence'][eid]['claim'])
        update(value['destination_asset'])
        self.ledger['evidence'][eid]['claim'] = json.dumps(value, ensure_ascii=False)

    def test_all_three_and_source_only_in_appendix(self):
        page = self.page()
        body = page.split('<article class="handbook">')[1].split('</article>')[0]
        self.assertIn('class="destination-card"', body)
        self.assertIn('src="https://example.test/east-gate.jpg"', body)
        self.assertNotIn('https://example.test/map', body)
        for label in ('看什么', '怎么走', '停多久', '何时最好', '坑在哪'):
            self.assertIn('<dt>' + label + '</dt>', body)
        self.assertEqual(body.count('class="daily-route"'), 2)
        self.assertEqual(body.count('class="daily-timeline"'), 2)
        self.assertIn('示意级精度、非导航图', body)
        self.assertIn('20分钟', body)
        self.assertIn('>1/3</text>', body)
        self.assertIn('https://example.test/map', page.split('class="records"')[1])
        for time in ('07:00', '09:00', '18:00', '08:00', '19:00'):
            self.assertEqual(body.count('<time>' + time + '</time>'), 1)

    def test_missing_image_remains_complete_text_card(self):
        self.asset(lambda a: a.update(images=[]))
        body = self.page().split('</article>')[0]
        self.assertNotIn('<img ', body)
        self.assertIn('class="destination-card"', body)
        self.assertIn('东门建筑', body)
        self.assertIn('img[hidden]{display:none}', body)

    def test_missing_partial_invalid_or_mixed_coordinates_fall_back(self):
        original = deepcopy(self.ledger)
        for coords in (None, {}, {'latitude': 91, 'longitude': 0, 'crs': 'WGS84'},
                       {'latitude': True, 'longitude': 0, 'crs': 'WGS84'},
                       {'latitude': 31, 'longitude': 121, 'crs': 'unknown'},
                       {'latitude': 31, 'longitude': 121, 'crs': 'GCJ02'}):
            with self.subTest(coords=coords):
                self.ledger = deepcopy(original)
                self.asset(lambda a: a.update(coordinates=coords))
                body = self.page().split('</article>')[0]
                first_day = body.split('第二天')[0]
                self.assertNotIn('<svg ', first_day)
                self.assertIn('测试酒店 → 测试景点 → 测试酒店', first_day)
                self.assertIn('class="daily-timeline"', first_day)

    def test_render_is_pure_and_revision_times_follow_body(self):
        before = deepcopy((self.snapshot, self.ledger))
        self.page()
        self.assertEqual((self.snapshot, self.ledger), before)
        self.snapshot['markdown'] = MARKDOWN.replace('09:00', '10:30')
        body = self.page().split('</article>')[0]
        self.assertIn('<time>10:30</time>', body)
        self.assertNotIn('<time>09:00</time>', body)

    def test_spaced_day_headings_and_missing_transport_duration(self):
        self.snapshot['markdown'] = MARKDOWN.replace('第一天', '第 1 天').replace('第二天', 'Day 2')
        body = self.page().split('</article>')[0]
        self.assertEqual(body.count('class="daily-timeline"'), 2)
        self.assertIn('耗时未提供，待确认', body)
        self.assertNotIn('540分钟', body)

    def test_table_columns_follow_headers_and_keep_full_duration(self):
        self.snapshot['markdown'] = """# 手册
## **第一天**
| 时刻 | 地点 | 事项 |
| --- | --- | --- |
| 07:00 | 测试酒店 | 起床 |
| 09:00 | 测试景点 | 车程约1小时30分钟后游览 |
"""
        page = self.page().split('</article>')[0]
        self.assertIn('class="daily-route"', page)
        self.assertIn('本站事项所载交通：车程约1小时30分钟', page)
        self.assertIn('<span class="event-place">测试景点</span>', page)
        self.snapshot['markdown'] = """# 手册
## 第一天
| 时间 | 活动 | 交通 | 地点 |
| --- | --- | --- | --- |
| 07:00 | 起床 | 无 | 测试酒店 |
| 09:00 | 游览 | 步行20分钟 | 测试景点 |
"""
        page = self.page().split('</article>')[0]
        self.assertIn('本站事项所载交通：步行20分钟', page)
        self.assertNotIn('<span class="event-place">步行20分钟', page)

    def test_outbound_duration_is_not_assigned_to_inbound_segment(self):
        self.snapshot['markdown'] = MARKDOWN.replace('从测试酒店到测试景点，乘车20分钟', '游览后乘车30分钟返回测试酒店')
        page = self.page().split('</article>')[0]
        self.assertNotIn('到此段', page)
        self.assertIn('本站事项所载交通：乘车30分钟（对应路段见正文）', page)

    def test_explicit_segment_binding_and_outbound_counterexample(self):
        page = self.page().split('</article>')[0]
        self.assertIn('1→2 测试酒店 → 测试景点：乘车20分钟', page)
        self.assertIn('2→3 测试景点 → 测试酒店：耗时待确认', page)
        self.snapshot['markdown'] = MARKDOWN.replace('从测试酒店到测试景点，乘车20分钟',
            '从测试景点到测试酒店，乘车30分钟')
        page = self.page().split('</article>')[0]
        self.assertIn('1→2 测试酒店 → 测试景点：耗时待确认', page)
        self.assertIn('2→3 测试景点 → 测试酒店：乘车30分钟', page)

    def test_qualified_segment_prose_never_binds(self):
        for text in ('并非从测试酒店到测试景点，乘车20分钟',
                     '晴天才从测试酒店到测试景点，步行20分钟',
                     '从测试酒店到测试景点，车程20分钟是错误估计，实际未知'):
            self.snapshot['markdown'] = MARKDOWN.replace('从测试酒店到测试景点，乘车20分钟', text)
            page = self.page().split('</article>')[0]
            self.assertIn('1→2 测试酒店 → 测试景点：耗时待确认', page)
            self.assertIn(text, page)

    def test_unsafe_images_text_and_malformed_assets(self):
        self.asset(lambda a: a.update(images=[{'url': 'javascript:alert(1)'},
                                              {'url': 'https://[bad'}, None]))
        self.snapshot['markdown'] += '\n<script>alert(1)</script>'
        page = self.page()
        self.assertNotIn('<img ', page)
        self.assertNotIn('<script>alert(1)</script>', page)
        self.ledger['evidence']['E1']['claim'] = '[]'
        self.page()

    def test_unknown_times_and_five_fields_not_invented(self):
        self.snapshot['markdown'] = '# 手册\n## 第一天 · 散步\n### 测试景点\n看什么：东门。'
        body = self.page().split('</article>')[0]
        self.assertNotIn('<time>', body)
        self.assertNotIn('<svg ', body)
        self.assertIn('正文未提供可识别时刻', body)
        self.assertEqual(body.count('正文未单列，待补充'), 4)


if __name__ == '__main__':
    unittest.main()
