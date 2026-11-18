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
                records.append(dict(kind='photo', name=spot['name'], attribution='Fixture Author · CC BY-SA',
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
        self.assertEqual(result['presentation_receipt']['status'], 'below_standard')
        failure = result['presentation_receipt']['degradations'][0]
        self.assertEqual(failure['field_path'], 'presentation.media')
        self.assertEqual(failure['rule'], 'stage_failure_isolation')

    def test_destination_reaches_provider_and_standard_reaches_receipt(self):
        class Anchored(Media):
            def enrich(inner, detail, destination=''):
                self.assertEqual(destination, '纽约 New York City')
                return super().enrich(detail)
        result = render_snapshot(self.snapshot(), dict(sources={}, evidence={}),
            intake={'destination': '纽约 New York City'}, media_provider=Anchored())
        standard = result['presentation_receipt']['delivery_standard']
        self.assertEqual(standard['maps'], 2)
        self.assertEqual(standard['photos'], 2)
        self.assertEqual(standard['gaps'], [])

    def test_freeform_destination_context_reaches_media_without_inference(self):
        intake = {'brief': '福冈三日带父母，慢节奏', 'dates': '2027-03-19~21',
                  'client_token': 'must-not-reach-media'}
        class Contextual(Media):
            def enrich(inner, detail, destination='', trip_context=None):
                self.assertEqual(destination, '')
                self.assertEqual(trip_context, {k: intake[k] for k in ('brief', 'dates')})
                return super().enrich(detail)
        render_snapshot(self.snapshot(), dict(sources={}, evidence={}),
                        intake=intake, media_provider=Contextual())

    def test_standard_three_states_without_document_mutation(self):
        from packages.travel_agent.single_session_presentation import delivery_standard
        detail = {'days': [{'attractions': [{'name': 'Original'}]}]}
        before = deepcopy(detail)
        self.assertEqual(delivery_standard(detail, [])['status'], 'below_standard')
        self.assertEqual(detail, before)
        detail['days'][0]['attractions'][0]['images'] = [{'url': 'https://example.com/photo.jpg', 'source_url': 'https://example.com/source'}]
        self.assertEqual(delivery_standard(detail, [])['status'], 'degraded')
        detail['days'][0]['_presentation_map_html'] = '<figure>map</figure>'
        self.assertEqual(delivery_standard(detail, [])['status'], 'ready')
        self.assertEqual(delivery_standard(detail, [{'rule': 'other_gap'}])['status'], 'degraded')

    def test_unrenderable_images_do_not_hide_zero_media(self):
        data = fixture()
        for day in data['days']:
            for spot in day['attractions']:
                spot['images'] = [{'url': 'javascript:alert(1)',
                                   'source_url': 'https://example.com/source'}]
        snapshot = self.snapshot()
        snapshot['markdown'] = markdown(data)
        result = render_snapshot(snapshot, dict(sources={}, evidence={}))
        self.assertNotIn('<img ', result['html'])
        self.assertEqual(result['presentation_receipt']['status'], 'below_standard')
        self.assertEqual(result['presentation_receipt']['delivery_standard']['photos'], 0)

    def test_saved_fields_cannot_inject_renderer_owned_html(self):
        data = fixture()
        data['days'][0]['_presentation_map_html'] = '<script>alert(1)</script>'
        snapshot = self.snapshot()
        snapshot['markdown'] = markdown(data)
        page = render_snapshot(snapshot, dict(sources={}, evidence={}))['html']
        self.assertNotIn('<script>alert(1)</script>', page)

    def test_optional_overview_shapes_do_not_block_presentation(self):
        from packages.travel_agent.single_session_presentation import accounting_view
        from copy import deepcopy
        for overview in (None, '原始说明', [], {'budget_tiers': None},
                         {'budget_tiers': [None, '说明', {'verified_items': None}]}):
            data = dict(overview=overview)
            original = deepcopy(data)
            prose, rendered, excluded = accounting_view('行程', data, {'evidence': {}})
            self.assertEqual(data, original)
            self.assertEqual(prose, '行程')
            self.assertIsInstance(rendered['overview'].get('budget_tiers', []), list)
            self.assertEqual(excluded, set())

    def test_unclosed_text_block_is_preserved_without_rejection(self):
        raw = '<section><p>未闭合的正文，保留原文'
        self.assertEqual(concentrate(raw), raw)

    def test_uncertainty_stays_local_and_separate_section_repeats_survive(self):
        raw = ('<section><h2>第一天</h2><p>测试酒店供暖待复核，落实前不要预订。</p>'
               '<p>测试码头末班待确认，未确认前不要锁定返程。</p>'
               '<p>测试庭院先参观东门，再沿主路前往出口。</p></section>'
               '<section><h2>概览</h2><p>测试庭院先参观东门，再沿主路前往出口。</p></section>'
               '<section class="trip-corrections"><h2>出发前先核这几条</h2></section>')
        result = concentrate(raw)
        body, pending = result.split('<section class="trip-corrections">')
        self.assertIn('供暖待复核', body)
        self.assertNotIn('测试酒店供暖待复核，落实前不要预订。', pending)
        self.assertIn('末班待确认', body)
        self.assertEqual(result.count('测试庭院先参观东门，再沿主路前往出口。'), 2)


if __name__ == '__main__':
    unittest.main()
