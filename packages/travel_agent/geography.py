"""Reusable spatial support for model-owned whole-trip decisions.

This is not a route optimizer. Distances are lower-bound straight-line distances,
never travel time, walkability or road distance; existing graph math is reused.
"""
import math

from packages.graph.edges import haversine_km


def located(item):
    lat, lng = item.get('lat'), item.get('lng')
    return (type(lat) in (int, float) and type(lng) in (int, float)
            and math.isfinite(lat) and math.isfinite(lng)
            and -90 <= lat <= 90 and -180 <= lng <= 180)


def closest(origin, candidates, limit):
    distances = [{'id': item['id'], 'name': item['name'],
                  'straight_line_km': round(haversine_km(origin['lat'], origin['lng'],
                                                       item['lat'], item['lng']), 1)}
                 for item in candidates if item['id'] != origin['id']]
    return sorted(distances, key=lambda item: item['straight_line_km'])[:limit]


def spatial_context(supplies):
    known = [s for s in supplies if located(s)]
    hotels = [s for s in known if s['kind'] in ('hotel', 'lodging', 'stay')]
    dining = [s for s in known if s['kind'] in ('dining', 'restaurant', 'food')]
    places = [s for s in known if s not in hotels and s not in dining and s['kind'] != 'transport']
    return {
        'basis': '由既有地点坐标计算的直线距离下界，不是路程/车程/可步行证明。历史坐标不是实时入口。',
        'planning_instruction': '先确定具体住宿基地，再按真实相对位置组合每日体验和餐饮。'
            '同属一座城市不代表附近；不可把一处区域的酒店称作另一片区域的住宿。'
            '已要求午休时，从上午终点回基地、用餐、足额休息、再去下午活动均需占用时间。'
            '无法查到车程时保守预留交通时间，明说规划缓冲，不编成地图实测。',
        'places': [{'id': s['id'], 'name': s['name'],
                    'closest_hotels': closest(s, hotels, 4),
                    'closest_dining': closest(s, dining, 3),
                    'closest_places': closest(s, places, 3)} for s in places],
        'unknown_location_ids': [s['id'] for s in supplies if not located(s)],
    }
