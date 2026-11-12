"""Versioned document content, not a new tool or HTTP contract.

Structured days are the sole rendered authority. Legacy documents keep their
existing adapter. No geometry, time or price is inferred from narrative.
"""
import html
import json
import math
import re
from . import single_session_context as context
from .single_session_media import video_html, media_text

BLOCK = re.compile(r'^```destination-detail-v1\s*\n(.*?)^```\s*$', re.M | re.S)
DAY = re.compile(r'^#{1,6}\s+(?:第\s*[一二三四五六七八九十\d]+\s*天|Day\s*\d+|D\d+)(?:\b|\s|[：:·｜|—-])', re.I)


def _fail(path, rule, message):
    raise ValueError(f'destination detail: {path} [{rule}] {message}')


def _objects(value, field, path='$'):
    items = value.get(field, [])
    if not isinstance(items, list):
        _fail(path + '.' + field, 'object_array', 'must be an object array')
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            _fail(f'{path}.{field}[{i}]', 'object_array', 'must be an object')
    return items


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _text(value):
    return str(value) if value is not None and value != '' else '未核验'


def _unique_ids(items, path):
    seen = set()
    for i, item in enumerate(items):
        value = item.get('id')
        if not isinstance(value, str) or not value.strip() or value in seen:
            _fail(f'{path}[{i}].id', 'unique_nonempty_id', 'IDs must be unique nonempty strings')
        seen.add(value)
    return seen


def _validate_route(owner, path):
    stops = _objects(owner, 'stops', path)
    ids = _unique_ids(stops, path + '.stops')
    for i, stop in enumerate(stops):
        if not isinstance(stop.get('name'), str) or not stop['name'].strip():
            _fail(f'{path}.stops[{i}].name', 'nonempty_text', 'stop name required')
    pairs = []
    for i, segment in enumerate(_objects(owner, 'segments', path)):
        pair = (segment.get('from'), segment.get('to'))
        for key in ('from', 'to'):
            if not isinstance(segment.get(key), str) or segment[key] not in ids:
                _fail(f'{path}.segments[{i}].{key}', 'stop_reference', 'invalid segment endpoint')
        if pair in pairs:
            _fail(f'{path}.segments[{i}]', 'unique_segment', 'duplicate segment endpoints')
        pairs.append(pair)
        for key in ('distance_km', 'duration_minutes'):
            if segment.get(key) is not None and not _number(segment[key]):
                _fail(f'{path}.segments[{i}].{key}', 'nonnegative_finite_number', 'invalid ' + key)


def _validate(data):
    if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1:
        _fail('$.version', 'supported_version', 'version must be 1')
    context.validate(data.get('overview'))
    days = _objects(data, 'days')
    if not days:
        _fail('$.days', 'nonempty_array', 'days must not be empty')
    _unique_ids(days, '$.days')
    for i, day in enumerate(days):
        path = f'$.days[{i}]'
        _validate_route(day, path)
        for j, event in enumerate(_objects(day, 'timeline', path)):
            time = event.get('time')
            if time is not None and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', str(time)):
                _fail(f'{path}.timeline[{j}].time', 'clock_hhmm', 'invalid event time')
            if event.get('duration_minutes') is not None and not _number(event['duration_minutes']):
                _fail(f'{path}.timeline[{j}].duration_minutes', 'nonnegative_finite_number', 'invalid event duration')
        for j, spot in enumerate(_objects(day, 'attractions', path)):
            spot_path = f'{path}.attractions[{j}]'
            _validate_route(spot, spot_path)
            _objects(spot, 'images', spot_path)
            for key in ('tips', 'missing'):
                if not isinstance(spot.get(key, []), list):
                    _fail(spot_path + '.' + key, 'array', key + ' must be an array')
        _objects(day, 'hotels', path)
        for j, dining in enumerate(_objects(day, 'dining', path)):
            price = dining.get('per_person')
            if price is not None:
                if not isinstance(price, str) or re.search(r'\d', re.sub(r'\{\{price:E[1-9]\d*\}\}', '', price)):
                    _fail(f'{path}.dining[{j}].per_person', 'price_evidence_marker',
                          'unbound price in per_person; use a price evidence marker')


def extract(markdown):
    """Return retained prose, structured payload, conservative self-check findings.

    Suppress whole old day sections, not guessed individual facts. The original
    source version and removed text remain available for review/recovery.
    """
    matches = list(BLOCK.finditer(markdown))
    if not matches:
        if '```destination-detail' in markdown:
            _fail('$.markdown.destination-detail-v1', 'closed_fenced_block',
                  'expected opening ```destination-detail-v1 and closing ``` on separate lines; closing fence missing or label unsupported')
        return markdown, None, []
    if len(matches) != 1:
        _fail('$.markdown.destination-detail-v1', 'single_block', 'exactly one block required')
    try:
        data = json.loads(matches[0][1])
    except json.JSONDecodeError as exc:
        _fail('$.markdown.destination-detail-v1', 'json_syntax',
              f'invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}')
    _validate(data)
    prose = markdown[:matches[0].start()] + markdown[matches[0].end():]
    lines, kept, findings, i = prose.splitlines(), [], [], 0
    while i < len(lines):
        if DAY.match(lines[i].replace('**', '')):
            level = len(lines[i]) - len(lines[i].lstrip('#'))
            end = i + 1
            while end < len(lines):
                h = re.match(r'^(#{1,6})\s+', lines[end])
                if h and len(h[1]) <= level:
                    break
                end += 1
            findings.append(dict(code='prose_day_replaced', line=i+1,
                                 removed='\n'.join(lines[i:end]), authority='destination-detail-v1'))
            i = end
        else:
            kept.append(lines[i])
            i += 1
    for day in data['days']:
        kinds = {e.get('kind') for e in day.get('timeline', [])}
        for kind in ('breakfast', 'lunch', 'dinner', 'hotel'):
            if kind not in kinds:
                findings.append(dict(code='missing_timeline_kind', day=day['id'], kind=kind))
        for event in day.get('timeline', []):
            if event.get('kind') == 'transport':
                segment = next((s for s in day.get('segments', [])
                                if s['from'] == event.get('from') and s['to'] == event.get('to')), None)
                if segment is None:
                    findings.append(dict(code='unbound_transport', day=day['id'], time=event.get('time')))
                elif event.get('duration_minutes') not in (None, segment.get('duration_minutes')):
                    findings.append(dict(code='transport_duration_conflict', day=day['id'], time=event.get('time'), authority='segment'))
        for spot in day.get('attractions', []):
            if not valid_images(spot) and not spot.get('missing'):
                findings.append(dict(code='missing_image_record', day=day['id'], place=spot.get('name')))
    return '\n'.join(kept).strip(), data, findings


def segment_text(segment, names):
    distance = (str(segment['distance_km']) + ' 千米' if segment.get('distance_km') is not None else '距离未核验')
    duration = (str(segment['duration_minutes']) + ' 分钟' if segment.get('duration_minutes') is not None else '时长未核验')
    return (names.get(segment.get('from'), '未知站点') + ' → ' + names.get(segment.get('to'), '未知站点')
            + '：' + distance + '；' + duration + '；' + _text(segment.get('status'))
            + '；' + _text(segment.get('basis')) + '；价格或班次：' + _text(segment.get('price_or_timetable')))


def event_text(event, owner=None):
    if event.get('kind') == 'transport' and owner is not None:
        segment = next((s for s in owner.get('segments', [])
                        if s['from'] == event.get('from') and s['to'] == event.get('to')), None)
        names = {s['id']: s['name'] for s in owner.get('stops', [])}
        if segment is not None:
            return _text(event.get('action')) + ' · ' + _text(event.get('place')) + ' · ' + segment_text(segment, names)
        return _text(event.get('action')) + ' · ' + _text(event.get('place')) + ' · 交通路段未绑定，距离/时长未核验'
    duration = str(event['duration_minutes']) + ' 分钟' if event.get('duration_minutes') is not None else '时长未核验'
    return _text(event.get('action')) + ' · ' + _text(event.get('place')) + ' · ' + duration


def valid_images(spot):
    from .single_session_document import _safe_url
    result = []
    for item in spot.get('images', []):
        try:
            safe = all(isinstance(item.get(k), str) and _safe_url(item[k])
                       for k in ('url', 'source_url'))
        except ValueError:
            safe = False
        if safe:
            result.append(item)
    return result[:2]


def spot_lines(spot):
    labels = [('see', '看什么'), ('route', '怎么走'), ('stay', '停多久'), ('best_time', '何时最好'), ('pitfall', '坑在哪')]
    result = [label + '：' + _text(spot.get(key)) for key, label in labels]
    result += ['玩法：' + _text(tip) for tip in spot.get('tips', [])]
    result += ['本季：' + _text(spot.get('season')), '其他季节：' + _text(spot.get('other_seasons'))]
    if not valid_images(spot):
        result += ['图片未提供：' + '；'.join(map(_text, spot.get('missing', []))) if spot.get('missing') else '图片未提供，缺失原因未记录']
    return result


def route_lines(owner):
    names = {s['id']: s['name'] for s in owner.get('stops', [])}
    result = ['路线：' + ' → '.join(names.values())] if names else ['路线站点未提供']
    result += [segment_text(s, names) for s in owner.get('segments', [])]
    for a, b in zip(owner.get('stops', []), owner.get('stops', [])[1:]):
        if not any(s['from'] == a['id'] and s['to'] == b['id'] for s in owner.get('segments', [])):
            result.append(a['name'] + ' → ' + b['name'] + '：距离未核验；时长未核验')
    return result


def day_markdown(day):
    lines = ['## ' + _text(day.get('title', day['id'])), _text(day.get('date')), _text(day.get('summary'))]
    lines += route_lines(day)
    lines += [_text(e.get('time')) + ' ' + event_text(e, day) for e in day.get('timeline', [])]
    for spot in day.get('attractions', []):
        lines += ['### ' + _text(spot.get('name'))] + spot_lines(spot) + route_lines(spot)
    lines += ['住宿：' + _text(h.get('name')) + ' · ' + _text(h.get('tier')) + ' · ' + _text(h.get('note')) for h in day.get('hotels', [])]
    lines += ['餐饮：' + _text(d.get('name')) + ' · ' + _text(d.get('category')) + ' · 人均：' + _text(d.get('per_person')) for d in day.get('dining', [])]
    return '\n\n'.join(lines)


def canonical(markdown):
    prose, data, findings = extract(markdown)
    if data is None:
        return prose, data, findings
    before, after = split_overview(prose)
    return '\n\n'.join([before, context.markdown(data.get('overview')), after]
                       + [day_markdown(day) for day in data['days']]), data, findings


def split_overview(prose):
    """Place structured overview immediately after the document title."""
    lines = prose.splitlines()
    if lines and re.match(r'^#\s+', lines[0]):
        return lines[0], '\n'.join(lines[1:]).strip()
    return '', prose


def _route_verified(segment):
    """Explicit confirmation only; never promote arbitrary status prose."""
    return str(segment.get('status', '')).strip().lower() in (
        '已核验', '已交叉核验', '已交叉确认', 'verified', 'confirmed')


def _route_pairs(owner):
    stops = owner.get('stops', [])
    names = {stop['id']: stop['name'] for stop in stops}
    segments = list(owner.get('segments', []))
    for a, b in zip(stops, stops[1:]):
        if not any(s['from'] == a['id'] and s['to'] == b['id'] for s in segments):
            segments.append({'from': a['id'], 'to': b['id']})
    return names, segments


def _route_line(segment, names):
    line = names.get(segment.get('from'), '未知站点') + ' → ' + names.get(segment.get('to'), '未知站点')
    values = []
    if _route_verified(segment):
        for key, unit in (('distance_km', ' 千米'), ('duration_minutes', ' 分钟')):
            if segment.get(key) is not None:
                values.append(str(segment[key]) + unit)
    if _route_verified(segment) and segment.get('basis'):
        values.append(str(segment['basis']))
    # Price markers have their own evidence contract, independent of route status.
    timetable = str(segment.get('price_or_timetable') or '')
    markers = re.findall(r'\{\{price:E[1-9]\d*\}\}', timetable)
    if _route_verified(segment) and timetable and not _timetable_missing(timetable):
        values.append(timetable)
    elif markers:
        values.append('费用/班次待确认：' + timetable)
    return line + ('：' + '；'.join(values) if values else '')


def _timetable_missing(value):
    return not value or any(word in value.lower() for word in ('未核', '待核', '未知', 'pending', 'unverified'))


def route_pending(data):
    """One actionable sentence per day; complete routes remain in day/records."""
    result = []
    for day in data.get('days', []):
        count, labels = 0, set()
        for owner in [day] + day.get('attractions', []):
            _, segments = _route_pairs(owner)
            for segment in segments:
                missing = [label for key, label in (('distance_km', '距离'),
                           ('duration_minutes', '时长'))
                           if segment.get(key) is None or not _route_verified(segment)]
                if (_timetable_missing(str(segment.get('price_or_timetable') or ''))
                        or not _route_verified(segment)):
                    missing.append('票价或班次')
                if missing:
                    count += 1
                    labels.update(missing)
        unbound = sum(event.get('kind') == 'transport' and not any(
            s.get('from') == event.get('from') and s.get('to') == event.get('to')
            for s in day.get('segments', [])) for event in day.get('timeline', []))
        if count or unbound:
            scope = str(day.get('title') or day['id'])
            fields = '、'.join(label for label in ('距离', '时长', '票价或班次') if label in labels)
            text = scope + '：出发前核对当天路线的' + fields if count else scope + '：出发前核对交通衔接'
            if unbound and count:
                text += '及未对应路线的交通衔接'
            result.append(text + '，确认前保留机动时间。')
    return result


def _event_line(event):
    parts = [str(event[key]) for key in ('action', 'place') if event.get(key)]
    # Travel measurements appear once in the route, not from conflicting events.
    if event.get('kind') != 'transport' and event.get('duration_minutes') is not None:
        parts.append(str(event['duration_minutes']) + ' 分钟')
    return ' · '.join(parts)


def _stay_lines(day, seen):
    result, missing = [], set()
    for field, label in (('hotels', '住宿'), ('dining', '餐饮')):
        items = day.get(field, [])
        if not items:
            continue
        identity = (field, json.dumps(items, ensure_ascii=False, sort_keys=True))
        if identity in seen:
            result.append(label + '：同前（' + seen[identity] + '）')
            continue
        seen[identity] = str(day.get('title') or day['id'])
        for item in items:
            keys = ('name', 'tier', 'note') if field == 'hotels' else ('name', 'category', 'per_person')
            parts = []
            for key in keys:
                value = str(item.get(key) or '')
                if key == 'per_person' and value in ('未核验', '待核验', '未知'):
                    missing.add('餐饮人均')
                    continue
                if key == 'note' and '房价未核验' in value:
                    missing.add('住宿房价')
                    value = value.replace('房价未核验', '').strip('，；,; ')
                if value:
                    parts.append(('人均：' if key == 'per_person' else '') + value)
            result.append(label + '：' + ' · '.join(parts))
    if missing:
        result.append('本日' + '、'.join(sorted(missing)) + '尚待确认，预订或点单前先问价。')
    return result


def route_html(owner, inline):
    from .single_session_document import _coordinate
    stops = owner.get('stops', [])
    coords = [_coordinate([s]) for s in stops]
    names, segments = _route_pairs(owner)
    lines = [_route_line(segment, names) for segment in segments]
    if not lines and names:
        lines = [' → '.join(names.values())]
    legend = ''.join('<p class="route-segment">' + inline(line) + '</p>' for line in lines)
    if len(coords) < 2 or any(c is None for c in coords) or len({c[2] for c in coords}) != 1:
        return '<div class="route-fallback">' + legend + '</div>'
    lats, lons = [c[0] for c in coords], [c[1] for c in coords]
    scale = 220 / max(max(lats)-min(lats), max(lons)-min(lons), .000001)
    points = [(160 + (c[1]-(max(lons)+min(lons))/2)*scale,
               140 - (c[0]-(max(lats)+min(lats))/2)*scale) for c in coords]
    marks = ''.join(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="14"/><text x="{x:.2f}" y="{y+4:.2f}" text-anchor="middle">{i}</text>' for i, (x, y) in enumerate(points, 1))
    return ('<figure class="daily-route"><figcaption>线路示意 · 非导航图</figcaption>'
            '<svg viewBox="0 0 320 280" role="img" aria-label="线路示意"><polyline points="'
            + ' '.join(f'{x:.2f},{y:.2f}' for x, y in points) + '"/>' + marks + '</svg>' + legend + '</figure>')


def render(data, inline):
    output, seen = [], {}
    for day in data['days']:
        output += ['<section class="itinerary-day"><header class="day-heading"><h2>' + inline(_text(day.get('title', day['id']))) + '</h2>']
        if day.get('date'):
            output += ['<p class="day-date">' + inline(str(day['date'])) + '</p>']
        if day.get('summary'):
            output += ['<p class="day-theme">' + inline(str(day['summary'])) + '</p>']
        output += ['</header><section class="day-movement"><h3>当天路线</h3>'
                   + day.get('_presentation_map_html', '') + route_html(day, inline) + '</section>']
        rows = ['<li><time>' + inline(str(e.get('time') or '时间待定')) + '</time><div>' + inline(_event_line(e)) + '</div></li>' for e in day.get('timeline', [])]
        output += ['<section class="daily-timeline"><h3>当日时间轴（建议安排）</h3><ol>' + ''.join(rows) + '</ol>' + ('' if rows else '<p>结构化时间轴未提供。</p>') + '</section>']
        for spot in day.get('attractions', []):
            pictures = []
            for item in valid_images(spot):
                caption = str(item.get('caption') or '拍摄时节见出处')
                if item.get('attribution'):
                    caption += ' · ' + str(item['attribution'])
                pictures.append('<figure><img loading="lazy" referrerpolicy="no-referrer" src="' + html.escape(item['url'], quote=True) + '" alt="' + html.escape(_text(spot.get('name')), quote=True) + '" onerror="this.hidden=true"><figcaption>' + media_text(caption, inline) + ' · <a rel="noreferrer noopener" href="' + html.escape(item['source_url'], quote=True) + '">图片出处</a></figcaption></figure>')
            lines = [line for line in spot_lines(spot) if not line.startswith('图片未提供')]
            output += ['<section class="destination-card">' + ''.join(pictures) + video_html(spot, inline) + '<div class="spot-copy"><h3>' + inline(_text(spot.get('name'))) + '</h3>' + ''.join('<p>' + inline(line) + '</p>' for line in lines) + route_html(spot, inline) + '</div></section>']
        output += ['<footer class="day-stay">' + ''.join('<p>' + inline(line) + '</p>' for line in _stay_lines(day, seen)) + '</footer></section>']
    return '\n'.join(output)
