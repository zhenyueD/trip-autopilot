from copy import deepcopy
from urllib.parse import urlsplit

import unittest

from packages.travel_agent.single_session_runtime_media import (
    COMMONS, GEOCODER, RuntimeMediaProvider, _fetch_json, _NoRedirect,
)


def handbook():
    return {'days': [{'day': 1, 'attractions': [{'name': 'Alpha Park'}, {'name': 'Beta Park'}],
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
    if endpoint == COMMONS:
        return photo(params['gsrsearch'].split('"')[1])
    assert endpoint == GEOCODER
    return [{'name': params['q'], 'lat': '30.25', 'lon': '120.1', 'osm_id': 1}]


def image_fetch(url):
    assert urlsplit(url).hostname in {'upload.wikimedia.org', 'tile.openstreetmap.org'}
    return 'image/png', b'fixture-raster'


def provider(fetcher=fetch, images=image_fetch):
    return RuntimeMediaProvider(fetch=fetcher, image_fetch=images)


def test_all_cards_days_attribution_and_no_mutation():
    source = handbook()
    before = deepcopy(source)
    output, records, failures = provider().enrich(source)
    assert source == before
    assert not failures
    assert len(output['days']) == 2
    assert len([r for r in records if r['kind'] == 'photo']) == 3
    for day in output['days']:
        assert 'class="handbook-map"' in day['_presentation_map_html']
        # Tiles are embedded as data URIs so the PDF renderer never re-fetches
        # them over a flaky proxy at print time.
        assert 'data:image/png;base64,' in day['_presentation_map_html']
        assert '<img ' in day['_presentation_map_html']
        for spot in day['attractions']:
            assert len(spot['images']) == 1
            assert spot['images'][0]['attribution'] == 'Fixture Photographer · CC BY-SA 4.0'


def test_photo_failure_only_removes_affected_card():
    def broken(endpoint, params):
        if endpoint == COMMONS and 'Alpha Park' in params['gsrsearch']:
            raise TimeoutError('photo timeout')
        return fetch(endpoint, params)
    output, records, failures = provider(broken).enrich(handbook())
    assert output['days'][0]['attractions'][0]['images'] == []
    assert output['days'][0]['attractions'][1]['images']
    assert output['days'][1]['attractions'][0]['images']
    assert failures[0]['field_path'] == '$.days[0].attractions[0].images'
    assert failures[0]['rule'] == 'photo_unavailable'


def test_geocode_failure_drops_only_affected_stop(mode):
    def broken(endpoint, params):
        if endpoint == GEOCODER and params['q'] == 'Alpha Park':
            if mode == 'failure':
                raise TimeoutError('geocoder timeout')
            if mode == 'no_match':
                return [{'name': 'Wrong Park', 'lat': 30, 'lon': 120}]
            if mode == 'nonfinite':
                return [{'name': 'Alpha Park', 'lat': 'nan', 'lon': 120}]
            raise AssertionError('ambiguous is covered by the anchor test')
        return fetch(endpoint, params)
    output, _, failures = provider(broken).enrich(handbook())
    # Per-stop degradation: the day keeps its map with the located stop plotted
    # and the failed stop named as unlocated; only the stop is lost, never the day.
    assert '_presentation_map_html' in output['days'][0]
    assert 'Beta Park' in output['days'][0]['_presentation_map_html']
    assert '未定位：Alpha Park' in output['days'][0]['_presentation_map_html']
    assert '_presentation_map_html' in output['days'][1]
    assert failures[0]['field_path'] == '$.days[0].stops[0]'
    assert failures[0]['stage'] == 'map'
    assert failures[0]['rule']


def test_geocode_ambiguous_resolves_near_the_day_anchor():
    # Alpha Park has two same-name candidates; only the one near Beta Park
    # (the day's located stop) is the day's stop. The far one must not win.
    def broken(endpoint, params):
        if endpoint == GEOCODER and params['q'] == 'Alpha Park':
            return [{'name': 'Alpha Park', 'lat': '30.25', 'lon': '120.1', 'osm_id': 1},
                    {'name': 'Alpha Park', 'lat': '40.0', 'lon': '116.0', 'osm_id': 2}]
        return fetch(endpoint, params)
    output, _, failures = provider(broken).enrich(handbook())
    assert '_presentation_map_html' in output['days'][0]
    assert 'Alpha Park' in output['days'][0]['_presentation_map_html']
    assert '未定位：Alpha Park' not in output['days'][0]['_presentation_map_html']
    assert not [f for f in failures if f['stage'] == 'map']


def test_geocode_wrong_city_candidate_stays_unlocated():
    # Regression (West Lake run 10, v2): 'Lake Dock' has two same-name entries
    # ~100 m apart in another city. Collapsing them in the single-stop context
    # plotted a Ningbo marker on a Hangzhou day. Only candidates near the
    # trip's located stops may resolve; the other day's Delta Park proves the
    # global anchor works across days.
    def fetcher(endpoint, params):
        if endpoint == GEOCODER:
            if params['q'] == 'Lake Dock (North)':
                return []
            if params['q'] == 'Lake Dock':
                return [{'name': 'Lake Dock', 'lat': '29.7842', 'lon': '121.6308', 'osm_id': 1},
                        {'name': 'Lake Dock', 'lat': '29.7850', 'lon': '121.6315', 'osm_id': 2}]
            if params['q'] == 'Delta Park（北门）':
                return []
            if params['q'] == 'Delta Park':
                return [{'name': 'Delta Park', 'lat': '30.25', 'lon': '120.10', 'osm_id': 1},
                        {'name': 'Delta Park', 'lat': '30.251', 'lon': '120.101', 'osm_id': 2}]
            return fetch(endpoint, params)
        return fetch(endpoint, params)
    detail = handbook()
    detail['days'][1]['stops'] = [{'name': 'Lake Dock (North)'}, {'name': 'Delta Park（北门）'}]
    output, _, failures = provider(fetcher).enrich(detail)
    html = output['days'][1].get('_presentation_map_html', '')
    assert 'Delta Park（北门）' in html
    assert '未定位：Lake Dock (North)' in html
    assert [f for f in failures if f['field_path'] == '$.days[1].stops[0]'
            and f['rule'] == 'geocode_ambiguous']


def test_geocode_ambiguous_far_from_anchor_stays_unlocated():
    # Both candidates sit in other cities: no anchor-relative pick is safe.
    def broken(endpoint, params):
        if endpoint == GEOCODER and params['q'] == 'Alpha Park':
            return [{'name': 'Alpha Park', 'lat': '40.0', 'lon': '116.0', 'osm_id': 1},
                    {'name': 'Alpha Park', 'lat': '39.0', 'lon': '117.0', 'osm_id': 2}]
        return fetch(endpoint, params)
    output, _, failures = provider(broken).enrich(handbook())
    assert '_presentation_map_html' in output['days'][0]
    assert '未定位：Alpha Park' in output['days'][0]['_presentation_map_html']
    assert failures[0]['field_path'] == '$.days[0].stops[0]'
    assert failures[0]['rule'] == 'geocode_ambiguous'


def test_geocode_failure_of_every_stop_drops_the_day_map():
    def broken(endpoint, params):
        if endpoint == GEOCODER:
            return []
        return fetch(endpoint, params)
    output, _, failures = provider(broken).enrich(handbook())
    assert all('_presentation_map_html' not in day for day in output['days'])
    assert any(f['rule'] == 'map_no_located_stops' for f in failures)


def test_photo_two_char_cjk_name_matches():
    detail = {'days': [{'day': 1, 'attractions': [{'name': '苏堤'}],
                        'stops': [{'name': '苏堤'}]}]}
    output, _, failures = provider().enrich(detail)
    assert output['days'][0]['attractions'][0]['images']
    assert not [f for f in failures if f['rule'] == 'photo_no_named_match']


def test_photo_category_name_matches():
    def fetcher(endpoint, params):
        if endpoint == COMMONS:
            return {'query': {'pages': [{'title': 'File:Something Else.jpg', 'imageinfo': [{
                'mime': 'image/jpeg',
                'thumburl': 'https://upload.wikimedia.org/Something_Else.jpg',
                'descriptionurl': 'https://commons.wikimedia.org/wiki/File:Something_Else.jpg',
                'extmetadata': {'Artist': {'value': '<a>Fixture Photographer</a>'},
                                'LicenseShortName': {'value': 'CC BY-SA 4.0'},
                                'ImageDescription': {'value': 'A view'},
                                'Categories': {'value': 'Alpha Park | China photographs taken on 2014-04-05'}}}]}]}}
        return fetch(endpoint, params)
    output, _, failures = provider(fetcher).enrich(handbook())
    assert output['days'][0]['attractions'][0]['images']
    assert output['days'][0]['attractions'][0]['images'][0]['attribution'] == 'Fixture Photographer · CC BY-SA 4.0'
    # The same page is returned for every spot: only the Alpha Park card matches
    # through its categories; the other two cards correctly report no match.
    assert len([f for f in failures if f['rule'] == 'photo_no_named_match']) == 2


def test_photo_base_name_fallback():
    def fetcher(endpoint, params):
        if endpoint == COMMONS:
            searched = params['gsrsearch'].split('"')[1]
            if '（' in searched:
                return photo('Other Place')
            return photo(searched)
        return fetch(endpoint, params)
    detail = handbook()
    detail['days'][0]['attractions'][0]['name'] = 'Gamma Park（北门）'
    output, _, failures = provider(fetcher).enrich(detail)
    image = output['days'][0]['attractions'][0]['images'][0]
    assert image['attribution'] == 'Fixture Photographer · CC BY-SA 4.0'
    assert image['caption'].startswith('Gamma Park（北门）')
    assert not [f for f in failures if f['rule'] == 'photo_no_named_match']


def test_geocode_empty_full_name_falls_back_to_base_matches():
    # Regression: a full name with zero matches must not swallow the base
    # name's matches — the first attempt's empty list is not a result.
    def fetcher(endpoint, params):
        if endpoint == GEOCODER:
            if params['q'] == 'Delta Park（北门）':
                return []
            if params['q'] == 'Delta Park':
                return [{'name': 'Delta Park', 'lat': '30.25', 'lon': '120.10', 'osm_id': 1},
                        {'name': 'Delta Park', 'lat': '30.251', 'lon': '120.101', 'osm_id': 2}]
            return fetch(endpoint, params)
        return fetch(endpoint, params)
    detail = handbook()
    detail['days'][0]['stops'][0]['name'] = 'Delta Park（北门）'
    output, _, failures = provider(fetcher).enrich(detail)
    assert '_presentation_map_html' in output['days'][0]
    assert 'Delta Park（北门）' in output['days'][0]['_presentation_map_html']
    assert not [f for f in failures if f['stage'] == 'map']


def test_geocode_base_name_and_same_place_collapse():
    def fetcher(endpoint, params):
        if endpoint == GEOCODER:
            if params['q'] == 'Gamma Park（北门）':
                return []
            if params['q'] == 'Gamma Park':
                return [{'name': 'Gamma Park', 'lat': '30.25', 'lon': '120.1', 'osm_id': 1}]
            if params['q'] == 'Alpha Park':
                return [{'name': 'Alpha Park', 'lat': '30.25', 'lon': '120.10', 'osm_id': 1},
                        {'name': 'Alpha Park', 'lat': '30.251', 'lon': '120.101', 'osm_id': 2}]
            return fetch(endpoint, params)
        return fetch(endpoint, params)
    detail = handbook()
    detail['days'][0]['attractions'][0]['name'] = 'Gamma Park（北门）'
    detail['days'][0]['stops'][0]['name'] = 'Gamma Park（北门）'
    detail['days'][1]['stops'][0]['name'] = 'Alpha Park'
    output, _, failures = provider(fetcher).enrich(detail)
    assert '_presentation_map_html' in output['days'][0]
    assert 'Gamma Park（北门）' in output['days'][0]['_presentation_map_html']
    assert '_presentation_map_html' in output['days'][1]
    assert 'Alpha Park' in output['days'][1]['_presentation_map_html']
    assert not [f for f in failures if f['stage'] == 'map']


def test_malicious_image_url_never_fetched(url):
    calls = []
    def broken(endpoint, params):
        payload = fetch(endpoint, params)
        if endpoint == COMMONS:
            payload['query']['pages'][0]['imageinfo'][0]['thumburl'] = url
        return payload
    def images(value):
        calls.append(value)
        return image_fetch(value)
    output, _, failures = provider(broken, images).enrich(handbook())
    assert url not in calls
    assert len([f for f in failures if f['rule'] == 'image_host']) == 3
    assert all(not spot['images'] for day in output['days'] for spot in day['attractions'])


def test_image_download_failure_is_card_local():
    def images(url):
        if 'Alpha_Park' in url:
            raise OSError('raster unavailable')
        return image_fetch(url)
    output, _, failures = provider(images=images).enrich(handbook())
    assert not output['days'][0]['attractions'][0]['images']
    assert output['days'][0]['attractions'][1]['images']
    assert failures[0]['stage'] == 'photo'


def test_name_mismatch_and_nonphoto_are_not_attached():
    def broken(endpoint, params):
        if endpoint == COMMONS:
            return photo('Different Park')
        return fetch(endpoint, params)
    _, _, failures = provider(broken).enrich(handbook())
    assert len([f for f in failures if f['rule'] == 'photo_no_named_match']) == 3


def test_construction_has_no_network_and_endpoint_rejected_before_network():
    RuntimeMediaProvider()
    with unittest.TestCase().assertRaisesRegex(ValueError, 'not allowed'):
        _fetch_json('https://localhost/private', {})


def test_tile_failure_drops_map_but_keeps_photos():
    def images(url):
        if 'tile.openstreetmap.org' in url:
            raise TimeoutError('tile unavailable')
        return image_fetch(url)
    output, _, failures = provider(images=images).enrich(handbook())
    assert all('_presentation_map_html' not in day for day in output['days'])
    assert all(spot['images'] for day in output['days'] for spot in day['attractions'])
    assert len(failures) == 2


class RuntimeMediaTests(unittest.TestCase):
    def test_success(self):
        test_all_cards_days_attribution_and_no_mutation()

    def test_photo_failure(self):
        test_photo_failure_only_removes_affected_card()

    def test_geocode_failures(self):
        for mode in ['failure', 'no_match', 'nonfinite']:
            with self.subTest(mode=mode):
                test_geocode_failure_drops_only_affected_stop(mode)

    def test_geocode_ambiguous_anchor(self):
        test_geocode_ambiguous_resolves_near_the_day_anchor()

    def test_geocode_ambiguous_far(self):
        test_geocode_ambiguous_far_from_anchor_stays_unlocated()

    def test_geocode_wrong_city(self):
        test_geocode_wrong_city_candidate_stays_unlocated()

    def test_geocode_all_stops_fail(self):
        test_geocode_failure_of_every_stop_drops_the_day_map()

    def test_photo_base_name(self):
        test_photo_base_name_fallback()

    def test_photo_two_char_cjk(self):
        test_photo_two_char_cjk_name_matches()

    def test_photo_category_name(self):
        test_photo_category_name_matches()

    def test_geocode_base_name_and_collapse(self):
        test_geocode_base_name_and_same_place_collapse()

    def test_geocode_empty_full_name_fallback(self):
        test_geocode_empty_full_name_falls_back_to_base_matches()

    def test_malicious_urls(self):
        for url in ['https://localhost/a.jpg', 'http://upload.wikimedia.org/a.jpg',
                    'https://upload.wikimedia.org.evil.test/a.jpg',
                    'https://user:pass@upload.wikimedia.org/a.jpg']:
            with self.subTest(url=url):
                test_malicious_image_url_never_fetched(url)

    def test_download_failure(self):
        test_image_download_failure_is_card_local()

    def test_name_mismatch(self):
        test_name_mismatch_and_nonphoto_are_not_attached()

    def test_nonphotographic_bitmap_rejected(self):
        def artwork(endpoint, params):
            result = fetch(endpoint, params)
            if endpoint == COMMONS:
                metadata = result['query']['pages'][0]['imageinfo'][0]['extmetadata']
                metadata['ImageDescription']['value'] = 'Painting of ' + params['gsrsearch'].split('"')[1]
            return result
        output, _, failures = provider(artwork).enrich(handbook())
        self.assertEqual(3, len([f for f in failures if f['rule'] == 'photo_no_named_match']))
        self.assertTrue(all(not s['images'] for d in output['days'] for s in d['attractions']))

    def test_redirect_refused(self):
        with self.assertRaisesRegex(ValueError, 'redirect refused'):
            _NoRedirect().redirect_request(None, None, 302, '', {}, 'https://localhost/private')

    def test_no_implicit_network(self):
        test_construction_has_no_network_and_endpoint_rejected_before_network()

    def test_tile_failure(self):
        test_tile_failure_drops_map_but_keeps_photos()


if __name__ == '__main__':
    unittest.main()
