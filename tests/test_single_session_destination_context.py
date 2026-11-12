"""Offline synthetic overview contracts, never destination fact evidence."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from packages.travel_agent import single_session_context as context
from packages.travel_agent.single_session_document import DocumentStore, render_snapshot


def fixture():
    tier = dict(name='经济', selected=False, lodging='合成简约住宿', vehicle='合成公共交通',
                dining='合成简餐', attractions='合成景点取舍',
                total_range=dict(lower='未核验', upper='未核验'), verified_items=[],
                estimated_items=['估算项金额未核验'], missing=['全部价格待核验'])
    selected = deepcopy(tier)
    selected.update(name='舒适', selected=True, lodging='合成舒适住宿', vehicle='合成包车')
    return dict(season_note=dict(travel_season='离线测试季节', scenery='景色未知', opening='开放未知',
                                price_level='价格档未知', daylight='日照长度未知', comparison='宣传图适用季节未知'),
                budget_tiers=[tier, selected],
                pace_note=dict(tradeoff='合成行程保留休息', one_day_less='删去合成支线', one_day_more='增加合成休息日'),
                time_context=dict(clock_basis='合成当地钟表口径', difference='时差尚未核验',
                                  meals='用餐作息未知', shops='商铺作息未知', timeline_note='时间轴与作息统一钟表口径',
                                  jet_lag='国际行程按到达日调整休息，合成提示',
                                  sun_events=[dict(method='unknown', basis='离线夹具未计算、未查表', sunrise=None, sunset=None)]))


def document(overview):
    data = dict(version=1, days=[dict(id='day-1', title='第一天', summary='离线合成休息日',
                                    timeline=[dict(kind='activity', action='休息')])])
    if overview is not None:
        data['overview'] = overview
    return '# 合成手册\n\n正文说明。\n\n```destination-detail-v1\n' + json.dumps(data, ensure_ascii=False) + '\n```\n'


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DocumentStore(self.tmp.name)
        self.overview = fixture()

    def save(self):
        result = self.store.save(document(self.overview))
        return result, Path(result['html_path']).read_text()

    def test_order_comparison_main_tier_and_unknown_sun(self):
        result, page = self.save()
        labels = ['<h1>合成手册', '<h2>这趟怎么定下来的', '正文说明',
                  '<section class="destination-overview">', '<h2>出发前准备与应急方案', '<h2>第一天',
                  '<h2>钱：价格与预算', '<h3>预算分档对照']
        indices = [page.index(label) for label in labels]
        self.assertEqual(indices, sorted(indices))
        self.assertEqual(page.count('舒适（主档）'), 1)
        for label in ('住宿', '车辆', '用餐', '景点取舍', '总额待确认；缺项尚未计入'):
            self.assertIn(label, page)
        self.assertIn('已核项：暂无已核项', page)
        self.assertIn('估算项：估算项金额待确认', page)
        self.assertIn('日出日落尚待核实', page)
        self.assertNotIn('（计算值）', page)
        self.assertIn('少一天：删去合成支线', result['rendered_markdown'])

    def test_no_time_context_and_legacy(self):
        self.overview['time_context'] = None
        result, page = self.save()
        self.assertNotIn('时差与当地作息', page)
        self.assertNotIn('倒时差', result['rendered_markdown'])
        legacy = self.store.save(document(None))
        self.assertNotIn('季节差异提醒', Path(legacy['html_path']).read_text())
        self.assertEqual(context.markdown(None), '')

    def test_raw_prices_and_missing_fields_rejected_before_save(self):
        for value in ('100', 'CNY 100', '一百元未核验', '{{price:E1}} + 100', '{{evidence:E1}}', '经济档', 100):
            self.overview['budget_tiers'][0]['total_range']['lower'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'unbound price'):
                self.save()
        self.assertIsNone(self.store.current())
        for section, key in [('season_note', 'daylight'), ('pace_note', 'one_day_more')]:
            value = fixture()
            del value[section][key]
            with self.assertRaises(ValueError):
                context.validate(value)

    def test_amount_items_cannot_bypass_price_binding(self):
        for key in ('verified_items', 'estimated_items'):
            for raw in ('80', '80/人', '80 至 100', '约 80', '住宿费用：100/人', '住宿费用100', '酒店100每晚'):
                value = fixture()
                value['budget_tiers'][0][key] = [raw]
                with self.subTest(key=key, raw=raw), self.assertRaisesRegex(ValueError, 'unbound price'):
                    self.store.save(document(value))
        value = fixture()
        value['budget_tiers'][0]['verified_items'] = ['2026-11-05 核验范围待确认']
        context.validate(value)
        value['time_context']['sun_events'][0].pop('basis')
        with self.assertRaises(ValueError):
            context.validate(value)

    def test_bound_range_uses_existing_ledger(self):
        self.store.register_source('s', 'https://example.test', 'Synthetic amounts 10 and 20', '2026-11-05')
        lower = self.store.record_evidence('s', '10', '合成价格下界', 'price', 10, 'CNY', 1)
        upper = self.store.record_evidence('s', '20', '合成价格上界', 'price', 20, 'CNY', 1)
        tier = self.overview['budget_tiers'][0]
        tier['total_range'] = dict(lower=lower['marker'], upper=upper['marker'])
        result, page = self.save()
        self.assertIn('10 元', page)
        self.assertIn('20 元', page)
        self.assertEqual(len(result['price_checks']), 2)

    def test_solar_metadata_required_and_clock_validation(self):
        event = dict(method='lookup', date='2026-11-05', timezone='Etc/UTC',
                     coordinates=dict(latitude=0, longitude=0, crs='WGS84'),
                     basis='合成格式测试值，非真实查表结果', sunrise='06:00', sunset='18:00')
        self.overview['time_context']['sun_events'] = [event]
        _, page = self.save()
        self.assertIn('（查表值）', page)
        self.assertIn('非真实查表结果', page)
        for key, invalid in [('timezone', 'Moon/Unknown'), ('date', '2026-99-99'),
                             ('coordinates', None), ('basis', ''), ('sunrise', '25:00'), ('method', 'unknown')]:
            broken = deepcopy(self.overview)
            broken['time_context']['sun_events'][0][key] = invalid
            with self.subTest(key=key), self.assertRaises(ValueError):
                context.validate(broken)

    def test_edit_reload_undo_and_html_escape(self):
        first, _ = self.save()
        changed = deepcopy(self.overview)
        changed['pace_note']['one_day_less'] = '<img src=x onerror=alert(1)>'
        receipt = dict(changes='调整合成节奏', judgment='测试', alternatives='原版', next_action='查看')
        second = self.store.edit(first['hash'], [dict(old=document(self.overview), new=document(changed))], receipt)
        page = Path(second['html_path']).read_text()
        self.assertNotIn('<img src=x', page)
        self.assertIn('&lt;img src=x', page)
        self.assertEqual(DocumentStore(self.tmp.name).current()['hash'], second['hash'])
        restored = self.store.undo(second['hash'])
        self.assertIn('少一天：删去合成支线', Path(restored['html_path']).read_text())

    def test_renderer_preserves_source(self):
        before = deepcopy(self.overview)
        self.save()
        self.assertEqual(self.overview, before)


if __name__ == '__main__':
    unittest.main()
