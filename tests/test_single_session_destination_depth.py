"""Offline destination-depth contract and pre-render diagnostics."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from packages.travel_agent.single_session_agent import (
    SYSTEM, SingleSessionAgent, body_attribution_findings,
)
from tests.test_single_session_agent import call


class DestinationDepthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.agent = SingleSessionAgent(Path(temporary.name) / 'session')
        for name in ('_model', '_search', '_read_uncached'):
            blocker = patch.object(self.agent, name, side_effect=AssertionError('external call'))
            blocker.start()
            self.addCleanup(blocker.stop)

    def test_generation_spec_all_five_requirements_and_factuality(self):
        groups = [
            ['确定性行家叙述', '来源与逐字引文只进', '判断融入叙述', '不设独立注脚/旁注'],
            ['中文＋英文＋目的地当地语言', '在地媒体与便民信息', '外文指南', '地图数据', '来源冲突'],
            ['具体点位与角度', '入口→内部动线→出口', '停多久', '光线/人流/班次及理由', '坑在哪'],
            ['出入口一级', '官方时刻表给分钟级', '时长', '距离＋分钟', '不许编分钟数'],
            ['1–2 张', 'URL 与出处', '站点坐标', '不下载图片', '不许拿无关图充数'],
            ['单源仍标未交叉确认', '不承诺未来人流', '严禁照填', '不猜坐标系'],
        ]
        for group in groups:
            for requirement in group:
                with self.subTest(requirement=requirement):
                    self.assertIn(requirement, SYSTEM)

    def test_blacklist_and_appendix_scope(self):
        for text in ['据本地宝，入口在东侧。', '官网显示入口在东侧。', '来源称需要预约。',
                     '根据官方介绍，入口在东侧。', '据旅行者，入口在东侧。',
                     '本地媒体显示东门开放。', '检索结果显示东门开放。']:
            with self.subTest(text=text):
                self.assertTrue(body_attribution_findings(text))
        self.assertEqual(body_attribution_findings('从东门进，走到湖边；班次待确认，未交叉确认。'), [])
        text = '# 手册\n## 核查记录\n来源称需要预约。\n### 引文\n据本地宝。\n## 第二天\n官网显示入口关闭。'
        self.assertEqual([x['line'] for x in body_attribution_findings(text)], [7])

    def diagnostic(self, tool_id):
        path = self.agent.directory / 'generation-checks' / (hashlib.sha256(tool_id.encode()).hexdigest() + '.json')
        return json.loads(path.read_text())

    def test_save_records_diagnostic_before_render_without_rewriting(self):
        import packages.travel_agent.single_session_document as document
        render = document.render_snapshot
        text = '# 行程\n据本地宝，入口在东侧。'

        def inspect(*args, **kwargs):
            check = self.diagnostic('save')
            self.assertEqual(check['phase'], 'before_render')
            self.assertEqual(check['findings'][0]['line'], 2)
            self.assertEqual(check['document_hash'], hashlib.sha256(text.encode()).hexdigest())
            return render(*args, **kwargs)

        with patch.object(document, 'render_snapshot', side_effect=inspect) as renderer:
            result = self.agent._execute_tool(call('save', 'save_handbook', markdown=text))
        self.assertTrue(result['document_saved'])
        self.assertEqual(renderer.call_count, 1)
        self.assertEqual(self.agent.documents.current()['markdown'], text)
        restarted = SingleSessionAgent(self.agent.directory)
        with patch.object(restarted.documents, 'save', side_effect=AssertionError('repeated save')):
            self.assertEqual(restarted._execute_tool(call('save', 'save_handbook', markdown=text)), result)
        self.assertEqual(self.agent.budget.snapshot()['used'], 0)

    def test_edit_checks_complete_candidate_before_render(self):
        self.agent._execute_tool(call('save', 'save_handbook', markdown='# 行程\n从东门进。'))
        current = self.agent.documents.current()
        args = dict(base_hash=current['hash'], edits=[dict(old='从东门进。', new='来源称从西门进。')],
                    receipt=dict(changes='调整入口', judgment='入口待确认', alternatives='暂缓', next_action='复核'))
        original = self.agent.documents.edit

        def inspect(**kwargs):
            self.assertEqual(self.diagnostic('edit')['findings'][0]['line'], 2)
            return original(**kwargs)

        with patch.object(self.agent.documents, 'edit', side_effect=inspect):
            result = self.agent._execute_tool(call('edit', 'edit_handbook', **args))
        self.assertTrue(result['document_saved'])
        args['base_hash'] = current['hash']
        result = self.agent._execute_tool(call('stale', 'edit_handbook', **args))
        self.assertTrue(result['not_completed'])
        self.assertEqual(self.agent.documents.current()['version'], 2)

    def test_assets_round_trip_in_existing_evidence_ledger(self):
        source_url = 'https://example.test/map'
        image_url = 'https://example.test/east-gate.jpg'
        quote = f'East gate photo {image_url}; coordinates 31.25, 121.5 WGS84.'
        self.agent.documents.register_source('S1', source_url, quote, '2026-11-05')
        asset = dict(destination_asset=dict(place='测试景点', point='东门',
            images=[dict(url=image_url, source_url=source_url)],
            coordinates=dict(latitude=31.25, longitude=121.5, crs='WGS84', source_url=source_url), missing=[]))
        result = self.agent._execute_tool(call('asset', 'record_evidence', source_id='S1',
            quote=quote, claim=json.dumps(asset, ensure_ascii=False), kind='other'))
        ledger_path = self.agent.documents.directory / 'ledger.json'
        ledger = json.loads(ledger_path.read_text())
        record = ledger['evidence'][result['evidence_id']]
        self.assertEqual(json.loads(record['claim']), asset)
        self.assertEqual(record['quote'], quote)
        self.assertEqual(record['url'], source_url)
        self.assertEqual(record['source_content_hash'], ledger['sources']['S1']['content_hash'])
        restored = SingleSessionAgent(self.agent.directory)
        self.assertEqual(restored.documents.evidence[result['evidence_id']], record)
        missing = dict(destination_asset=dict(place='测试景点', point='西门', images=[],
                       coordinates=None, missing=['对应图片与坐标未知']))
        quote2 = 'West gate; no image or coordinates provided.'
        self.agent.documents.register_source('S2', source_url + '/west', quote2, '2026-11-05')
        result2 = self.agent._execute_tool(call('missing', 'record_evidence', source_id='S2',
            quote=quote2, claim=json.dumps(missing, ensure_ascii=False), kind='other'))
        self.assertEqual(json.loads(result2['claim']), missing)
        rejected = self.agent._execute_tool(call('fake', 'record_evidence', source_id='S2',
            quote='invented image URL', claim=json.dumps(asset), kind='other'))
        self.assertTrue(rejected['not_completed'])
        self.assertEqual(len(self.agent.documents.evidence), 2)
        self.assertEqual(self.agent.budget.snapshot()['used'], 0)


if __name__ == '__main__':
    unittest.main()
