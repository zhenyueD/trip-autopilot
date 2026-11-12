"""Google navigation handoff, without fabricating geometry or running OSM routes."""
from urllib.parse import urlencode


def route_links(plan, supplies):
    by_id = {s['id']: s for s in supplies if isinstance(s, dict) and s.get('id')}
    output = []
    for day in plan.get('days', []):
        items = day.get('items', [])
        named = [by_id.get(item.get('supply_id'), {}) for item in items]
        query = next((s.get('name') for s in named if s.get('name')), day.get('city') or day.get('title', ''))
        output.append({'day': day['day'], 'status': 'unavailable', 'provider': 'Google Maps',
                       'mode': 'unspecified', 'geometry': None, 'legs': [],
                       'source_url': 'https://www.google.com/maps/search/?' + urlencode({'api': 1, 'query': query}),
                       'note': '地图与站点由Google Maps展示；实时交通方式与导航需在Google Maps选择。未计算路线几何或时长。'})
    return output
