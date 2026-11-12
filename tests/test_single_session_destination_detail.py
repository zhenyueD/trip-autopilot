"""Synthetic offline contract tests; no destination facts or external traffic."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from packages.travel_agent.single_session_document import DocumentStore, render_snapshot
from packages.travel_agent.single_session_detail import canonical


def fixture():
    stops = [dict(id='a', name='测试酒店', coordinates=dict(latitude=31, longitude=121, crs='WGS84', source_url='https://example.test/map')),
             dict(id='b', name='测试景点', coordinates=dict(latitude=31.1, longitude=121.1, crs='WGS84', source_url='https://example.test/map'))]
    segments = [dict(**{'from': 'a', 'to': 'b'}, distance_km=12, duration_minutes=25, status='离线合成数据', basis='测试夹具，非旅行事实', price_or_timetable='未核验')]
    timeline = [dict(time=t, kind=k, action=a, place='测试景点', duration_minutes=n) for t,k,a,n in [
        ('08:30','breakfast','早餐：酒店用餐',30),('09:00','transport','乘车前往景点',25),('09:30','activity','游览庭院',90),('12:00','lunch','午餐：测试餐馆',60),('18:00','dinner','晚餐：自主安排',60),('20:00','hotel','入住测试酒店',None)]]
    timeline[1].update({'from':'a','to':'b'})
    spot = dict(name='测试景点',see='庭院的光影',route='东门→庭院→西门',stay='建议90分钟',best_time='上午，测试光线条件',pitfall='出口不可当入口',tips=['先走庭院','再看东门'],season='本季看庭院',other_seasons='其他季节待核',images=[],missing=['未找到对应图片'],stops=deepcopy(stops),segments=deepcopy(segments))
    return dict(version=1, days=[dict(id='day-1',title='第一天',date='2026-11-05',summary='慢行庭院。',stops=stops,segments=segments,timeline=timeline,attractions=[spot],hotels=[dict(name='测试酒店',tier='同档',note='房价未核验')],dining=[dict(name='测试餐馆',category='面食',per_person='未核验')])])


def markdown(data, prose='# 离线合成夹具\n'):
    return prose + '\n```destination-detail-v1\n' + json.dumps(data,ensure_ascii=False,indent=2) + '\n```\n'


class DetailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DocumentStore(self.tmp.name)
        self.data = fixture()

    def save(self, prose='# 离线合成夹具\n'):
        result = self.store.save(markdown(self.data,prose))
        return result, Path(result['html_path']).read_text()

    def test_structured_route_timeline_card_and_missing_image(self):
        result,page = self.save()
        self.assertEqual(page.count('class="daily-route"'),2)
        self.assertIn('<time>08:30</time>',page)
        self.assertNotIn('12 千米；25 分钟',page.split('</article>')[0])
        self.assertIn('12 千米；25 分钟',canonical(result['markdown'])[0])
        self.assertIn('class="destination-card"',page)
        self.assertIn('未找到对应图片',page)
        self.assertNotIn('<img ',page)
        self.assertNotIn('destination-detail-v1',page.split('</article>')[0])
        self.assertIn('destination-detail-v1',page.split('</article>')[1])
        self.assertIn('早餐',result['rendered_markdown'])

    def test_conflict_structured_wins_and_audit_is_persisted(self):
        result,page = self.save('# 夹具\n## 第一天\n06:00 旧行程错误数据\n### 测试景点\n停多久：999分钟\n## 行前准备\n携带证件。\n')
        self.assertNotIn('999分钟',page.split('</article>')[0])
        self.assertIn('999分钟',page.split('</article>')[1])
        self.assertNotIn('旧行程错误数据',canonical(result['markdown'])[0])
        self.assertIn('携带证件',page)
        check=json.loads((Path(self.tmp.name)/('detail-check-'+result['hash']+'.json')).read_text())
        self.assertEqual(check['findings'][0]['code'],'prose_day_replaced')
        self.assertIn('999分钟',check['findings'][0]['removed'])
        self.assertIn('999分钟',result['markdown'])

    def test_edit_undo_and_reload_preserve_single_authority(self):
        first,_=self.save()
        changed=deepcopy(self.data)
        changed['days'][0]['timeline'][0]['time']='08:45'
        receipt=dict(changes='调整早餐',judgment='更从容',alternatives='原时间',next_action='查看')
        second=self.store.edit(first['hash'],[dict(old=markdown(self.data),new=markdown(changed))],receipt)
        self.assertIn('<time>08:45</time>',Path(second['html_path']).read_text())
        self.assertEqual(DocumentStore(self.tmp.name).current()['hash'],second['hash'])
        restored=self.store.undo(second['hash'])
        self.assertIn('<time>08:30</time>',Path(restored['html_path']).read_text())

    def test_coordinates_and_distances_do_not_get_inferred(self):
        day=self.data['days'][0]
        day['stops'][0]['coordinates']=None
        day['segments'][0]['distance_km']=None
        day['segments'][0]['duration_minutes']=None
        day['attractions']=[]
        _,page=self.save()
        self.assertNotIn('<svg ',page)
        self.assertIn('距离、时长',page)
        self.assertIn('出发前准备',page)
        self.assertIn('<time>09:00</time>',page)

    def test_bad_structure_never_falls_back_to_prose(self):
        original=deepcopy(self.data)
        for mutation in (lambda d:d.update(version=2),lambda d:d['days'][0]['timeline'][0].update(time='25:00'),lambda d:d['days'][0]['segments'][0].update(distance_km=-3),lambda d:d['days'][0]['segments'][0].update(to='missing')):
            data=deepcopy(original);mutation(data)
            with self.assertRaises(ValueError):self.store.save(markdown(data))
        with self.assertRaises(ValueError):self.store.save('```destination-detail-v1\nnot json\n```')
        self.assertIsNone(self.store.current())

    def test_price_cannot_bypass_guard_in_structure(self):
        for price in ('CNY 80', 80, '80', '80/人', '{{price:E1}} + 80'):
            self.data['days'][0]['dining'][0]['per_person']=price
            with self.subTest(price=price), self.assertRaisesRegex(ValueError,'unbound price'):
                self.save()
        self.store.register_source('s','https://example.test','CNY 80 per person','2026-11-05')
        ev=self.store.record_evidence('s','CNY 80','餐饮','price',80,'CNY',2)
        self.data['days'][0]['dining'][0]['per_person']=ev['marker']
        result,page=self.save()
        self.assertIn('160 元',page)
        self.assertEqual(len(result['price_checks']),1)

    def test_images_require_source_safe_urls_and_text_is_escaped(self):
        spot=self.data['days'][0]['attractions'][0]
        spot['see']='<script>alert(1)</script>'
        spot['images']=[dict(url='javascript:alert(1)',source_url='https://example.test')]
        _,page=self.save()
        self.assertNotIn('<img ',page)
        self.assertNotIn('<script>alert(1)</script>',page)
        spot['images']=[dict(url='https://example.test/photo.jpg',source_url='https://example.test/source')]
        _,page=self.save()
        self.assertIn('src="https://example.test/photo.jpg"',page)
        self.assertIn('href="https://example.test/source"',page)

    def test_transport_uses_bound_segment_and_reports_conflict(self):
        self.data['days'][0]['timeline'][1]['duration_minutes']=999
        result,page=self.save()
        timeline=page.split('class="daily-timeline"')[1].split('</section>')[0]
        self.assertNotIn('12 千米；25 分钟',timeline)
        self.assertIn('12 千米；25 分钟',canonical(result['markdown'])[0])
        self.assertNotIn('999 分钟',timeline)
        self.assertIn('transport_duration_conflict', (Path(self.tmp.name)/('detail-check-'+result['hash']+'.json')).read_text())

    def test_diagnostics_and_render_purity(self):
        self.data['days'][0]['timeline']=[]
        text=markdown(self.data)
        _,_,findings=canonical(text)
        self.assertEqual(sum(x['code']=='missing_timeline_kind' for x in findings),4)
        before=deepcopy(self.data)
        self.save()
        self.assertEqual(before,self.data)


if __name__=='__main__':unittest.main()
