"""Offline provider regressions with explicit model decisions; no real model calls."""
from copy import deepcopy
from urllib.parse import urlsplit
import unittest
from unittest.mock import patch

from packages.travel_agent.single_session_runtime_media import (
    COMMONS, GEOCODER, WIKIPEDIA, OPENVERSE, RuntimeMediaProvider, _fetch_json, _NoRedirect,
    place_name_candidates,
)


def handbook():
    return {'days': [{'day': 1, 'city': 'Fixture City', 'date': '2026-11-13',
                      'attractions': [{'name': 'Alpha Park'}, {'name': 'Beta Park'}],
                      'stops': [{'name': 'Alpha Park'}, {'name': 'Beta Park'}]},
                     {'day': 2, 'attractions': [{'name': 'Gamma Park'}],
                      'stops': [{'name': 'Gamma Park'}]}]}


def photo(name):
    return {'query': {'pages': [{'title': 'File:' + name + '.jpg', 'imageinfo': [{
        'mime': 'image/jpeg', 'thumburl': 'https://upload.wikimedia.org/' + name.replace(' ', '_') + '.jpg',
        'descriptionurl': 'https://commons.wikimedia.org/wiki/File:' + name.replace(' ', '_') + '.jpg',
        'extmetadata': {'Artist': {'value': '<a>Fixture Photographer</a>'},
                        'LicenseShortName': {'value': 'CC BY-SA 4.0'},
                        'ImageDescription': {'value': 'Photograph of ' + name}}}]}]}}


def fetch(endpoint, params):
    if endpoint == OPENVERSE:
        return {'results': []}
    if endpoint == COMMONS:
        return photo(params.get('gsrsearch', 'Fixture').split('"')[1]
                     if '"' in params.get('gsrsearch', '') else 'Fixture')
    if endpoint in WIKIPEDIA.values():
        return {'query': {'pages': []}}
    assert endpoint == GEOCODER
    return [{'name': params['q'], 'lat': '30.25', 'lon': '120.1', 'osm_id': 1}]


def image_fetch(url):
    # Selected photos now acquire bytes before projection, alongside map tiles.
    assert urlsplit(url).hostname in {'tile.openstreetmap.org', 'upload.wikimedia.org'}
    return 'image/png', b'fixture-raster'


def model_choice(context):
    """A deliberate fixture choice, not a production matching algorithm."""
    def first(place, kind):
        return place[kind][0]['id'] if place[kind] else None
    return {'anchor': {'coordinate_id': first(context['anchor'], 'coordinates'),
                       'reason': 'Fixture model anchor decision'},
            'places': [{'place_id': place['id'],
                        'coordinate_id': first(place, 'coordinates'),
                        'photo_id': first(place, 'photos'),
                        'reason': 'Fixture model selected or declined supplied evidence'}
                       for place in context['places']]}, {'tokens': {'total_tokens': 17}}


def provider(fetcher=fetch, images=image_fetch, model=model_choice):
    return RuntimeMediaProvider(fetch=fetcher, image_fetch=images, model_client=model)


class RuntimeMediaTests(unittest.TestCase):
    def test_success_preserves_source_and_records_model_usage(self):
        source = handbook()
        before = deepcopy(source)
        output, records, failures = provider().enrich(source)
        self.assertEqual(before, source)
        self.assertFalse(failures)
        self.assertEqual(3, len([r for r in records if r['kind'] == 'photo']))
        calls = [r for r in records if r['kind'] == 'model_call']
        self.assertEqual(1, len(calls))
        self.assertEqual(17, calls[0]['usage']['tokens']['total_tokens'])
        selection = next(r for r in records if r['kind'] == 'media_selection')
        self.assertEqual('2026-11-13', selection['candidates']['days'][0]['date'])
        for day in output['days']:
            self.assertIn('class="handbook-map"', day['_presentation_map_html'])
            self.assertIn('data:image/png;base64,', day['_presentation_map_html'])
            for spot in day['attractions']:
                self.assertEqual(1, len(spot['images']))
                self.assertEqual('Fixture Photographer · CC BY-SA 4.0', spot['images'][0]['attribution'])

    def test_photo_failure_is_card_local_and_preserved_in_acquisition_audit(self):
        def broken(endpoint, params):
            if endpoint == COMMONS and 'Alpha Park' in params.get('gsrsearch', ''):
                raise TimeoutError('photo timeout')
            return fetch(endpoint, params)
        output, records, failures = provider(broken).enrich(handbook())
        self.assertFalse(output['days'][0]['attractions'][0]['images'])
        self.assertTrue(output['days'][0]['attractions'][1]['images'])
        self.assertTrue(output['days'][1]['attractions'][0]['images'])
        self.assertTrue(any(f['field_path'] == '$.days[0].attractions[0].images'
                            and f['rule'] == 'media_model_none' for f in failures))
        snapshot = next(r['candidates'] for r in records if r['kind'] == 'media_selection')
        self.assertTrue(snapshot['places'][0]['failures'])

    def test_geocode_failure_is_stop_local(self):
        for mode in ('failure', 'empty', 'nonfinite', 'model_rejection'):
            def broken(endpoint, params):
                if endpoint == GEOCODER and 'Alpha Park' in params['q']:
                    if mode == 'failure':
                        raise TimeoutError('geocoder timeout')
                    if mode == 'empty':
                        return []
                    return [{'name': 'Wrong Park', 'lat': 'nan' if mode == 'nonfinite' else 40,
                             'lon': 120}]
                return fetch(endpoint, params)
            def choose(context):
                result, usage = model_choice(context)
                if mode == 'model_rejection':
                    for place, row in zip(context['places'], result['places']):
                        if place['name'] == 'Alpha Park':
                            row['coordinate_id'] = None
                            row['reason'] = 'The candidate is the wrong entity'
                return result, usage
            with self.subTest(mode=mode):
                output, _, failures = provider(broken, model=choose).enrich(handbook())
                html = output['days'][0]['_presentation_map_html']
                self.assertIn('Beta Park', html)
                self.assertIn('未定位：Alpha Park', html)
                self.assertIn('_presentation_map_html', output['days'][1])
                self.assertTrue(any(f['field_path'] == '$.days[0].stops[0]' for f in failures))

    def test_all_stops_missing_drops_maps_only(self):
        def broken(endpoint, params):
            return [] if endpoint == GEOCODER else fetch(endpoint, params)
        output, _, failures = provider(broken).enrich(handbook())
        self.assertTrue(all('_presentation_map_html' not in d for d in output['days']))
        self.assertTrue(all(s['images'] for d in output['days'] for s in d['attractions']))
        self.assertTrue(any(f['rule'] == 'map_no_located_stops' for f in failures))

    def test_non_https_or_credentialed_urls_are_rejected_without_photo_download(self):
        for url in ('http://upload.wikimedia.org/a.jpg',
                    'https://user:pass@upload.wikimedia.org/a.jpg'):
            def broken(endpoint, params):
                result = fetch(endpoint, params)
                if endpoint == COMMONS:
                    result['query']['pages'][0]['imageinfo'][0]['thumburl'] = url
                return result
            with self.subTest(url=url):
                output, _, failures = provider(broken).enrich(handbook())
                self.assertEqual(3, len([f for f in failures if f['rule'] == 'image_url']))
                self.assertTrue(all(not s['images'] for d in output['days'] for s in d['attractions']))

    def test_photo_technical_requirements_survive_model_selection(self):
        for missing in ('Artist', 'LicenseShortName', 'mime'):
            def broken(endpoint, params):
                result = fetch(endpoint, params)
                if endpoint == COMMONS:
                    info = result['query']['pages'][0]['imageinfo'][0]
                    if missing == 'mime':
                        info['mime'] = 'image/svg+xml'
                    else:
                        del info['extmetadata'][missing]
                return result
            with self.subTest(missing=missing):
                output, _, failures = provider(broken).enrich(handbook())
                self.assertTrue(all(not s['images'] for d in output['days'] for s in d['attractions']))
                self.assertEqual(3, len([f for f in failures if f['rule'] == 'photo_provenance']))

    def test_tile_failure_drops_maps_but_keeps_photos(self):
        def images(url):
            if urlsplit(url).hostname == 'tile.openstreetmap.org':
                raise TimeoutError('tile unavailable')
            return image_fetch(url)
        output, _, failures = provider(images=images).enrich(handbook())
        self.assertTrue(all('_presentation_map_html' not in d for d in output['days']))
        self.assertTrue(all(s['images'] for d in output['days'] for s in d['attractions']))
        self.assertEqual(2, len(failures))

    def test_redirect_refused(self):
        with self.assertRaisesRegex(ValueError, 'redirect refused'):
            _NoRedirect().redirect_request(None, None, 302, '', {}, 'https://localhost/private')

    def test_construction_no_network_and_endpoint_rejected(self):
        RuntimeMediaProvider()
        with self.assertRaisesRegex(ValueError, 'not allowed'):
            _fetch_json('https://localhost/private', {})


class CandidateMediaTests(unittest.TestCase):
    def test_missing_anchor_does_not_discard_independently_selected_places(self):
        def choose(context):
            result, usage = model_choice(context)
            result['anchor'] = {'coordinate_id': None, 'reason': 'Destination uncertain'}
            return result, usage
        output, records, failures = provider(model=choose).enrich(handbook(), 'Unknown City')
        self.assertTrue(all('_presentation_map_html' in d for d in output['days']))
        self.assertTrue(all(s['images'] for d in output['days'] for s in d['attractions']))
        self.assertTrue(any(f['field_path'] == '$.destination' for f in failures))
        self.assertFalse(any(r['kind'] == 'destination_anchor' for r in records))

    def test_anchor_distance_is_annotation_and_state_is_enrichment_local(self):
        def fetcher(endpoint, params):
            if endpoint == GEOCODER and params['q'] == 'New York':
                return [{'name': 'New York', 'lat': 40.7, 'lon': -74}]
            return fetch(endpoint, params)
        shared = provider(fetcher)
        first, records, failures = shared.enrich(handbook(), 'New York')
        self.assertFalse(failures)
        self.assertTrue(all('_presentation_map_html' in d for d in first['days']))
        self.assertTrue(all(r['beyond_50_km'] for r in records if r['kind'] == 'coordinate'))
        _, records, failures = shared.enrich(handbook())
        self.assertFalse(failures)
        self.assertTrue(all('anchor_distance_km' not in r for r in records if r['kind'] == 'coordinate'))

    def test_mixed_names_are_query_variants(self):
        for name, expected in [
            ('时代广场 Times Square（夜景）', ['时代广场', 'Times Square']),
            ('Whitehall Terminal 白厅码头', ['Whitehall Terminal', '白厅码头']),
            ('南华茶室 Nom Wah Tea Parlor', ['南华茶室', 'Nom Wah Tea Parlor']),
        ]:
            candidates = place_name_candidates(name)
            self.assertEqual(name, candidates[0])
            self.assertTrue(set(expected) <= set(candidates))
        self.assertEqual([], place_name_candidates('https://evil.test'))

    def test_pageimage_candidate_retains_provenance_for_model(self):
        calls = []
        def fetcher(endpoint, params):
            calls.append((endpoint, params))
            if endpoint in WIKIPEDIA.values():
                if params.get('titles') != 'Times Square' or endpoint != WIKIPEDIA['en']:
                    return {'query': {'pages': []}}
                return {'query': {'pages': [{'title': 'Times Square', 'pageimage': 'Square.jpg',
                    'coordinates': [{'lat': 40.758, 'lon': -73.985}]}]}}
            if endpoint == COMMONS:
                return photo('Square') if 'titles' in params else {'query': {'pages': []}}
            return []
        detail = {'days': [{'day': 1, 'attractions': [{'name': '时代广场 Times Square'}]}]}
        output, records, failures = provider(fetcher).enrich(detail)
        self.assertFalse(failures)
        result = output['days'][0]['attractions'][0]['images'][0]
        self.assertTrue(result['caption'].startswith('时代广场 Times Square'))
        self.assertEqual('Fixture Photographer · CC BY-SA 4.0', result['attribution'])
        self.assertEqual('model_selected_candidate', result['match_basis'])
        self.assertIn('en.wikipedia.org', result['article_source_url'])
        self.assertTrue(any(p.get('titles') == 'File:Square.jpg' for _, p in calls))
        self.assertTrue(any(r['kind'] == 'media_selection' for r in records))

    def test_model_none_never_falls_back_to_saved_images(self):
        detail = handbook()
        detail['days'][0]['attractions'][0]['images'] = [{'url': 'https://upload.wikimedia.org/old.jpg'}]
        before = deepcopy(detail)
        def choose(context):
            result, usage = model_choice(context)
            for row in result['places']:
                row.update(coordinate_id=None, photo_id=None, reason='All candidates unsuitable')
            return result, usage
        output, _, failures = provider(model=choose).enrich(detail)
        self.assertEqual(before, detail)
        self.assertTrue(all(not s['images'] for d in output['days'] for s in d['attractions']))
        self.assertTrue(all('_presentation_map_html' not in d for d in output['days']))
        self.assertTrue(any(f['rule'] == 'media_model_none' for f in failures))


class RevisionMediaWorkTests(unittest.TestCase):
    """Exercise real enrich/choose seams with captured, offline source decisions."""
    def setUp(self):
        self.detail = {'days': [{'city': 'Fixture City', 'attractions': [{'name': 'Alpha Park'}]}]}
        self.acquired, self.model_calls = [], []
        self.collector = patch('packages.travel_agent.single_session_media_candidates.collect_candidates',
                               self.collect)
        self.collector.start()
        self.addCleanup(self.collector.stop)
        self.maps = patch.object(RuntimeMediaProvider, '_map', return_value='<figure>map</figure>')
        self.maps.start()
        self.addCleanup(self.maps.stop)

    def collect(self, fetcher, name, destination='', **kwargs):
        self.acquired.append((name, kwargs))
        return {'coordinates': [{'lat': 30, 'lon': 120, 'source': 'nominatim',
                                 'source_url': 'https://geo.example/north', 'raw': {'osm_id': 23}}],
                'photos': [{'url': 'https://upload.wikimedia.org/Alpha.png',
                            'source_url': 'https://commons.wikimedia.org/wiki/File:Alpha.png',
                            'source': 'wikimedia_commons', 'mime': 'image/png', 'attribution': 'Author',
                            'license': 'CC BY', 'raw': {'title': 'File:Alpha.png'}}], 'failures': []}

    def model(self, context):
        self.model_calls.append(deepcopy(context))
        return model_choice(context)

    def history(self, records, version):
        audit = [row for row in records if row['kind'] == 'media_selection'][-1]
        return {'context': deepcopy(audit['candidates']), 'decision': deepcopy(audit['decision']),
                'records': deepcopy(records), 'version': version,
                'source_snapshot_sha256': str(version) * 64}

    def complementary_histories(self):
        def missing_coordinate(context):
            decision, usage = model_choice(context)
            decision['places'][0]['coordinate_id'] = None
            return decision, usage
        _, records, _ = provider(model=missing_coordinate).enrich(self.detail)
        first = self.history(records, 1)

        def recovered_coordinate(context):
            decision, usage = model_choice(context)
            decision['places'][0]['photo_id'] = None  # Already bound by receipt reuse.
            return decision, usage
        output, records, failures = provider(model=recovered_coordinate).with_receipts([first]).enrich(self.detail)
        self.assertFalse(failures)
        self.assertTrue(output['days'][0]['attractions'][0]['images'])
        self.acquired.clear()
        return [self.history(records, 2), first]

    def test_copy_revision_combines_existing_fields_without_requery_or_model(self):
        histories = self.complementary_histories()
        detail = deepcopy(self.detail)
        detail['days'][0]['attractions'][0]['description'] = 'Changed ordinary prose only'
        output, records, failures = provider(model=self.model).with_receipts(histories).enrich(detail)
        self.assertEqual([], self.acquired, 'Independent source-bound fields must survive later revisions')
        self.assertEqual([], self.model_calls)
        self.assertFalse(failures)
        self.assertEqual('https://upload.wikimedia.org/Alpha.png',
                         output['days'][0]['attractions'][0]['images'][0]['url'])
        links = [r for r in records if r['kind'] == 'media_reuse' and r['place_id'] == 'p0']
        self.assertEqual([(2, ['coordinates']), (1, ['photos'])],
                         [(r['version'], r['kinds']) for r in links])

    def test_combined_reuse_invalidates_changed_identity_or_image_input(self):
        histories = self.complementary_histories()
        for key, value in [('city', 'Another City'), ('source_url', 'https://place.example/south'),
                           ('images', [{'url': 'https://images.example/new.jpg'}])]:
            with self.subTest(field=key):
                self.acquired.clear()
                self.model_calls.clear()
                detail = deepcopy(self.detail)
                detail['days'][0]['attractions'][0][key] = value
                _, records, _ = provider(model=self.model).with_receipts(histories).enrich(detail)
                self.assertEqual(1, len(self.acquired))
                self.assertEqual(1, len(self.model_calls))
                self.assertFalse(any(r['kind'] == 'media_reuse' for r in records))

    def test_missing_or_mismatched_photo_still_needs_selection_and_keeps_coordinates(self):
        histories = self.complementary_histories()
        for mutation in ('missing', 'receipt_mismatch'):
            with self.subTest(mutation=mutation):
                changed = deepcopy(histories)
                if mutation == 'missing':
                    changed[1]['decision']['places'][0]['photo_id'] = None
                else:
                    next(r for r in changed[1]['records'] if r['kind'] == 'photo')['license'] = 'different'
                self.model_calls.clear()
                with patch('packages.travel_agent.single_session_media_candidates.collect_candidates',
                           return_value={}) as collect:
                    output, records, failures = provider(model=self.model).with_receipts(changed).enrich(self.detail)
                self.assertEqual(1, collect.call_count)
                self.assertEqual(1, len(self.model_calls))
                self.assertFalse(output['days'][0]['attractions'][0]['images'])
                self.assertTrue(any(r['kind'] == 'coordinate' for r in records))
                self.assertTrue(any(r['stage'] == 'photo' for r in failures))

    def test_independent_rewrite_queries_overlap_and_preserve_place_decisions(self):
        import threading
        barrier = threading.Barrier(2)
        rewrites = []

        def collect(fetcher, name, destination='', **kwargs):
            if not kwargs.get('queries'):
                return {'coordinates': [], 'photos': [], 'failures': []}
            rewrites.append(kwargs['queries'][0])
            barrier.wait(timeout=1)
            return self.collect(fetcher, name, destination, **kwargs)

        def model(context):
            decision, usage = model_choice(context)
            if context['round'] == 1:
                for place, row in zip(context['places'], decision['places']):
                    row['rewrite_query'] = place['name'] + ' exact fixture'
            return decision, usage

        detail = deepcopy(self.detail)
        detail['days'][0]['attractions'].append({'name': 'Beta Park'})
        with patch('packages.travel_agent.single_session_media_candidates.collect_candidates', collect):
            output, records, failures = provider(model=model).enrich(detail)
        self.assertFalse(failures)
        self.assertCountEqual(['Alpha Park exact fixture', 'Beta Park exact fixture'], rewrites)
        self.assertTrue(all(spot['images'] for spot in output['days'][0]['attractions']))
        self.assertEqual([1, 2], [r['round'] for r in records if r['kind'] == 'model_call'])


if __name__ == '__main__':
    unittest.main()
