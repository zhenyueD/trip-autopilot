"""Offline product-render contract, synthetic facts and injected media only."""
from copy import deepcopy
import unittest

from packages.travel_agent.single_session_document import render_snapshot
from packages.travel_agent.single_session_presentation_copy import concentrate
from tests.test_single_session_destination_detail import fixture, markdown
from tests.test_single_session_destination_context import fixture as overview


class Media:
    def enrich(self, detail):
        result = deepcopy(detail)
        records = []
        for day in result['days']:
            day['_presentation_map_html'] = '<figure class="handbook-map">OSM 合成夹具</figure>'
            for spot in day['attractions']:
                spot['images'] = [dict(url='https://upload.wikimedia.org/fixture.jpg',
                    source_url='https://commons.wikimedia.org/wiki/File:fixture.jpg',
                    caption='离线图片', attribution='Fixture Author · CC BY-SA')]
                records.append(dict(place=spot['name'], attribution='Fixture Author · CC BY-SA',
                                    source_url=spot['images'][0]['source_url']))
        return result, records, []


class PresentationWireTests(unittest.TestCase):
    def snapshot(self):
        data = fixture()
        day = deepcopy(data['days'][0])
        day.update(id='day-2', title='第二天', date='2026-11-06')
        data['days'].append(day)
        data['overview'] = overview()
        return dict(markdown=markdown(data, '# 夹具\n## 预算分档\n旧分档冗余\n'),
            receipt=dict(changes='保存', judgment='离线', alternatives='修订', next_action='查看'))

    def test_all_nodes_budget_once_records_and_source_immutable(self):
        snapshot = self.snapshot()
        before = deepcopy(snapshot)
        result = render_snapshot(snapshot, dict(sources={}, evidence={}), media_provider=Media())
        body = result['html'].split('</article>')[0]
        self.assertEqual(snapshot, before)
        self.assertEqual(body.count('class="itinerary-day"'), 2)
        self.assertEqual(body.count('class="handbook-map"'), 2)
        self.assertEqual(body.count('class="budget-comparison"'), 1)
        self.assertNotIn('旧分档冗余', body)
        self.assertEqual(body.count('class="destination-card"'), 2)
        self.assertIn('<img ', body)
        self.assertIn('Fixture Author', body)
        records = result['html'].split('<details class="records">')[1]
        self.assertIn('commons.wikimedia.org', records)
        self.assertIn('Fixture Author', records)
        self.assertIn('本会话暂无核查记录', records)
        self.assertEqual(result['presentation_receipt']['degradations'][0]['field_path'], 'ledger.evidence')

    def test_provider_stage_failure_delivers_document(self):
        class Broken:
            def enrich(self, detail):
                raise RuntimeError('injected')
        result = render_snapshot(self.snapshot(), dict(sources={}, evidence={}), media_provider=Broken())
        self.assertIn('第二天', result['html'])
        self.assertEqual(result['presentation_receipt']['status'], 'degraded')
        failure = result['presentation_receipt']['degradations'][0]
        self.assertEqual(failure['field_path'], 'presentation.media')
        self.assertEqual(failure['rule'], 'stage_failure_isolation')

    def test_saved_fields_cannot_inject_renderer_owned_html(self):
        data = fixture()
        data['days'][0]['_presentation_map_html'] = '<script>alert(1)</script>'
        snapshot = self.snapshot()
        snapshot['markdown'] = markdown(data)
        page = render_snapshot(snapshot, dict(sources={}, evidence={}))['html']
        self.assertNotIn('<script>alert(1)</script>', page)

    def test_uncertainty_moves_whole_claim_s1_stays_and_duplicates_collapse(self):
        raw = ('<section><h2>第一天</h2><p>测试酒店供暖待复核，落实前不要预订。</p>'
               '<p>测试码头末班待确认，未确认前不要锁定返程。</p>'
               '<p>测试庭院先参观东门，再沿主路前往出口。</p></section>'
               '<section><h2>概览</h2><p>测试庭院先参观东门，再沿主路前往出口。</p></section>'
               '<section class="trip-corrections"><h2>出发前先核这几条</h2></section>')
        result = concentrate(raw)
        body, pending = result.split('<section class="trip-corrections">')
        self.assertNotIn('供暖待复核', body)
        self.assertIn('测试酒店供暖待复核，落实前不要预订。', pending)
        self.assertIn('末班待确认', body)
        self.assertEqual(result.count('测试庭院先参观东门，再沿主路前往出口。'), 1)


if __name__ == '__main__':
    unittest.main()
