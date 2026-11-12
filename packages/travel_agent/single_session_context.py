"""Overview content for destination-detail-v1; no research or price inference.

All displayed facts are supplied by the writer. This validates shape and binding,
not the truth of source claims. Legacy documents can omit the overview entirely.
"""
from datetime import date
from html import escape
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SEASON = (('travel_season', '出行季节'), ('scenery', '景色'), ('opening', '开放'),
          ('price_level', '价格档'), ('daylight', '日照长度'), ('comparison', '与宣传图或旺季的差异'))
CHOICES = (('lodging', '住宿'), ('vehicle', '车辆'), ('dining', '用餐'), ('attractions', '景点取舍'))
PACE = (('tradeoff', '本程取舍'), ('one_day_less', '少一天'), ('one_day_more', '多一天'))
TIME = (('clock_basis', '统一钟表口径'), ('difference', '时差或太阳时差异'),
        ('meals', '用餐作息'), ('shops', '商铺作息'), ('timeline_note', '时间轴安排'))
PRICE = re.compile(r'\{\{price:E[1-9]\d*\}\}')
CLOCK = re.compile(r'(?:[01]\d|2[0-3]):[0-5]\d')


def _fail(message, path, rule):
    raise ValueError(f'destination overview: {message} at {path} [{rule}]')


def _text(value):
    return value if isinstance(value, str) and value.strip() else '未核验'


def _fields(obj, fields, path):
    if not isinstance(obj, dict):
        _fail('expected object', path, 'object_type')
    for key, _ in fields:
        if not isinstance(obj.get(key), str) or not obj[key].strip():
            _fail(key + ' must be nonempty text', path + '.' + key, 'nonempty_text')


def _price(value, path):
    if not isinstance(value, str):
        _fail('unbound price; use a price evidence marker', path, 'price_evidence_marker')
    if PRICE.fullmatch(value):
        return
    if (re.search(r'\d|[零一二三四五六七八九十百千万亿两〇]', value)
            or not re.search(r'未核验|待核验|未知|pending-verification', value)
            or '{{' in value):
        _fail('unbound price; use a price evidence marker or unverified text', path, 'price_evidence_marker')


def _sun(event, path):
    if not isinstance(event, dict):
        _fail('sun event must be an object', path, 'object_type')
    method = event.get('method')
    if method == 'unknown':
        _fields(event, [('basis', '')], path)
        if event.get('sunrise') is not None or event.get('sunset') is not None:
            key = 'sunrise' if event.get('sunrise') is not None else 'sunset'
            _fail('unknown sun event must not assert a clock time', path + '.' + key, 'unknown_sun_no_clock')
        return
    if method not in ('calculated', 'lookup'):
        _fail('sun method must be calculated, lookup or unknown', path + '.method', 'sun_method')
    _fields(event, [('date', ''), ('timezone', ''), ('basis', '')], path)
    try:
        date.fromisoformat(event['date'])
    except ValueError:
        _fail('sun event needs valid date and IANA timezone', path + '.date', 'iso_date')
    try:
        ZoneInfo(event['timezone'])
    except (ValueError, ZoneInfoNotFoundError):
        _fail('sun event needs valid date and IANA timezone', path + '.timezone', 'iana_timezone')
    coords = event.get('coordinates')
    if not isinstance(coords, dict) or coords.get('crs') not in ('WGS84', 'WGS-84'):
        _fail('sun event needs WGS84 coordinates',
              path + '.coordinates' + ('.crs' if isinstance(coords, dict) else ''), 'wgs84_coordinates')
    for key, limit in [('latitude', 90), ('longitude', 180)]:
        number = coords.get(key)
        if (isinstance(number, bool) or not isinstance(number, (int, float))
                or not math.isfinite(number) or not -limit <= number <= limit):
            _fail('invalid sun coordinates', path + '.coordinates.' + key, 'coordinate_range')
    for key in ('sunrise', 'sunset'):
        value = event.get(key)
        if value is not None and (not isinstance(value, str) or not CLOCK.fullmatch(value)):
            _fail('invalid ' + key + ' clock time', path + '.' + key, 'clock_time')


def validate(overview):
    """Validate supplied overview; None preserves legacy document behavior."""
    if overview is None:
        return
    if not isinstance(overview, dict):
        _fail('overview must be an object', '$.overview', 'object_type')
    _fields(overview.get('season_note'), SEASON, '$.overview.season_note')
    _fields(overview.get('pace_note'), PACE, '$.overview.pace_note')
    tiers = overview.get('budget_tiers')
    if not isinstance(tiers, list) or not 2 <= len(tiers) <= 3:
        _fail('budget_tiers must contain two or three tiers', '$.overview.budget_tiers', 'tier_count')
    names, selected = [], 0
    for index, tier in enumerate(tiers):
        path = f'$.overview.budget_tiers[{index}]'
        _fields(tier, (('name', ''),) + CHOICES, path)
        names.append(tier['name'])
        if type(tier.get('selected')) is not bool:
            _fail('selected must be a boolean', path + '.selected', 'boolean_type')
        selected += tier['selected']
        bounds = tier.get('total_range')
        if not isinstance(bounds, dict):
            _fail('total_range must be an object', path + '.total_range', 'object_type')
        for key in ('lower', 'upper'):
            _price(bounds.get(key), path + '.total_range.' + key)
        for key in ('verified_items', 'estimated_items', 'missing'):
            items = tier.get(key)
            if not isinstance(items, list):
                _fail(key + ' must be a text array', path + '.' + key, 'text_array')
            for item_index, item in enumerate(items):
                if not isinstance(item, str) or not item.strip():
                    _fail(key + ' must be a text array',
                          f'{path}.{key}[{item_index}]', 'nonempty_text')
            if key != 'missing':
                for item_index, item in enumerate(items):
                    # Amount-bearing fields allow numeric literals only in
                    # bound prices and explicit ISO evidence dates.
                    bare = re.sub(r'\b\d{4}-\d{2}-\d{2}\b', '', PRICE.sub('', item))
                    if re.search(r'\d', bare):
                        _fail('unbound price in ' + key + '; use a price evidence marker',
                              f'{path}.{key}[{item_index}]', 'price_evidence_marker')
    if selected > 1:
        index = [i for i, tier in enumerate(tiers) if tier['selected']][1]
        _fail('tier names must be unique; select at most one tier',
              f'$.overview.budget_tiers[{index}].selected', 'single_selected_tier')
    if len(set(names)) != len(names):
        index = next(i for i, name in enumerate(names) if name in names[:i])
        _fail('tier names must be unique; select at most one tier',
              f'$.overview.budget_tiers[{index}].name', 'unique_tier_name')
    time = overview.get('time_context')
    if time is None:
        return
    _fields(time, TIME, '$.overview.time_context')
    if time.get('jet_lag') is not None and not isinstance(time['jet_lag'], str):
        _fail('jet_lag must be text or null', '$.overview.time_context.jet_lag', 'nullable_text')
    events = time.get('sun_events')
    if not isinstance(events, list) or not events:
        _fail('sun_events must include an entry or an explicit unknown entry',
              '$.overview.time_context.sun_events', 'nonempty_array')
    for index, event in enumerate(events):
        _sun(event, f'$.overview.time_context.sun_events[{index}]')


def _lines(obj, fields):
    return [label + '：' + _text(obj.get(key)) for key, label in fields]


def _budget_summary(tier):
    bounds = tier['total_range']
    return (tier['name'] + ('（主档）' if tier['selected'] else '') + ' · '
            + '；'.join(_lines(tier, CHOICES)) + '；总额区间：'
            + bounds['lower'] + ' 至 ' + bounds['upper'])


def _budget_details(tier):
    return [label + '：' + ('；'.join(tier[key]) or fallback)
            for key, label, fallback in [('verified_items', '已核项', '暂无已核项'),
                                        ('estimated_items', '估算项', '暂无估算项'),
                                        ('missing', '待核缺口', '未列明，需确认覆盖范围')]]


def _time_lines(time):
    lines = _lines(time, TIME)
    for event in time['sun_events']:
        if event['method'] == 'unknown':
            lines.append('日出 / 日落：未知，未提供有依据的钟表时间；' + _text(event.get('basis')))
            continue
        coord = event['coordinates']
        method = '计算值' if event['method'] == 'calculated' else '查表值'
        lines.append(event['date'] + ' · 日出 ' + _text(event.get('sunrise'))
                     + ' / 日落 ' + _text(event.get('sunset')) + '（' + method + '）'
                     + '；坐标：' + str(coord['latitude']) + ', ' + str(coord['longitude'])
                     + ' ' + coord['crs'] + '；时区：' + event['timezone']
                     + '；依据：' + event['basis'])
    if time.get('jet_lag'):
        lines.append('倒时差：' + time['jet_lag'])
    return lines


def markdown(overview):
    if overview is None:
        return ''
    validate(overview)
    lines = ['## 季节差异提醒'] + _lines(overview['season_note'], SEASON)
    lines += ['## 预算分档对照']
    for tier in overview['budget_tiers']:
        lines += ['### ' + _budget_summary(tier)] + _budget_details(tier)
    lines += ['## 行程节奏'] + _lines(overview['pace_note'], PACE)
    if overview.get('time_context') is not None:
        lines += ['## 时差与当地作息'] + _time_lines(overview['time_context'])
    return '\n\n'.join(lines)


# These are presentation choices only. Full source fields remain in canonical
# Markdown and the folded record archive; never truncate a factual sentence.
_ENGINEERING = re.compile(r'坐标|算法|海拔|时区代码|太阳正午|WGS.?84|GCJ.?02|Asia/|Etc/')


def _concise(values, limit=150):
    selected, size = [], 0
    for value in dict.fromkeys(v.strip() for v in values if isinstance(v, str) and v.strip()):
        if _ENGINEERING.search(value) or size + len(value) + 1 > limit:
            continue
        selected.append(value)
        size += len(value) + 1
    return '；'.join(selected)


def sun_summary(time):
    """Combine only same-place, same-clock, same-method observations, no inference."""
    events = time.get('sun_events', [])
    known = [e for e in events if e.get('method') in ('calculated', 'lookup')]
    if len(known) != len(events) or not known:
        return '日出日落尚待核实，勿据此锁定活动时间。'
    scopes = {(tuple(sorted(e.get('coordinates', {}).items())), e.get('timezone'), e.get('method')) for e in known}
    if len(scopes) != 1 or any(not e.get(k) for e in known for k in ('sunrise', 'sunset')):
        return '日照按日期与地点分别适用，明细见核查记录。'
    def clock_range(key):
        values = sorted(e[key] for e in known)
        return values[0] if values[0] == values[-1] else values[0] + '–' + values[-1]
    dates = sorted(e['date'] for e in known)
    scope = dates[0] if dates[0] == dates[-1] else dates[0] + '至' + dates[-1]
    method = '计算值' if known[0]['method'] == 'calculated' else '查表值'
    return scope + '所列日期、同一研究点：日出 ' + clock_range('sunrise') + '，日落 ' + clock_range('sunset') + '（' + method + '）。'


def render(overview, inline):
    """Compact decision copy; detailed budget is rendered beside the price table."""
    if overview is None:
        return ''
    validate(overview)
    season = overview['season_note']
    pace = overview['pace_note']
    opening = _concise([season['opening']], 90)
    season_copy = (_concise([season[k] for k in ('opening', 'travel_season', 'comparison', 'scenery', 'daylight', 'price_level')], 90)
                   if opening else '进入与开放条件见下方出行前待办，确认前不要锁定游览。')
    choices = [('季节', season_copy),
               ('节奏', _concise([pace['tradeoff'], '少一天：' + pace['one_day_less'], '多一天：' + pace['one_day_more']], 90))]
    time = overview.get('time_context')
    if time:
        clock = re.sub(r'[（(]UTC[+-]\d{2}:\d{2},\s*[A-Za-z_]+/[A-Za-z_/]+[）)]', '', time['clock_basis'])
        choices.append(('时差与当地作息', clock + '；' + _concise([sun_summary(time), time['difference'], time['timeline_note']], max(0, 110 - len(clock) - 1))))
    return '<section class="destination-overview">' + ''.join(
        '<p><strong>' + label + '</strong> · ' + inline(copy or '具体条件见核查记录。') + '</p>'
        for label, copy in choices) + '</section>'


def render_budget(overview, inline):
    if overview is None:
        return ''
    result = ['<section class="budget-comparison"><h3>预算分档对照</h3>']
    for tier in overview['budget_tiers']:
        # Financial amounts and qualifications stay intact, never shortened.
        result.append('<div class="budget-tier"><h4>' + inline(tier['name'] + ('（主档）' if tier['selected'] else '')) + '</h4>')
        result.append('<p>' + inline(_concise(_lines(tier, CHOICES), 140)) + '</p>')
        bounds = tier['total_range']
        # Collapse duplicate unknown bounds only; bound amounts retain their
        # original text and continue through the existing price renderer.
        total = ('总额待确认；缺项尚未计入。'
                 if bounds['lower'] == bounds['upper'] == '未核验'
                 else '总额区间：' + bounds['lower'] + ' 至 ' + bounds['upper'])
        result.append('<p>' + inline(total) + '</p>')
        result.extend('<p>' + inline(line) + '</p>' for line in _budget_details(tier))
        for currency, amount in tier.get('_verified_subtotals', {}).items():
            result.append('<p class="verified-subtotal">已核小计：' + escape(amount + ' ' + currency) + '（未含待核缺项）</p>')
        result.append('</div>')
    return ''.join(result) + '</section>'


def pending_conditions(overview):
    if overview is None:
        return []
    opening = overview['season_note']['opening']
    if not _concise([opening], 90):
        return ['进入与开放条件（' + overview['season_note']['travel_season'] + '）：' + opening]
    return []
