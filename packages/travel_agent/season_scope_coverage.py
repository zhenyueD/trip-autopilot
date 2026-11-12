"""Conservative, offline scope accounting; never rewrites a source atom's scope."""
from datetime import date, timedelta
import re


_SEASONS = {'春季': ((3, 1), (5, 31)), '夏季': ((6, 1), (8, 31)),
            '秋季': ((9, 1), (11, 30)), '冬季': ((12, 1), (2, 28))}
_DATE = r'\d{4}-\d{2}-\d{2}'


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(_DATE, value):
        raise ValueError('expected ISO date')
    return date.fromisoformat(value)


def _season_intervals(season, years):
    intervals = []
    for year in years:
        if season == '冬季':
            intervals.extend([(date(year, 1, 1), date(year, 3, 1) - timedelta(days=1)),
                (date(year, 12, 1), date(year, 12, 31))])
        else:
            start, end = _SEASONS[season]
            intervals.append((date(year, *start), date(year, *end)))
    return intervals


def _parse_scope(text, start, end):
    """Full-match grammar: unrecognized qualifications are never discarded."""
    if text in ('全年', '常年'):
        return 'all_dates', [(date.min, date.max)]
    match = re.fullmatch(rf'({_DATE})起常设', text)
    if match:
        return 'permanent_since', [(_date(match[1]), date.max)]
    match = re.fullmatch(rf'(\d{{4}})(?:\(公示日期({_DATE})\))?', text)
    if match:
        year = int(match[1])
        if match[2] and _date(match[2]).year != year:
            raise ValueError('publication year conflicts with scope year')
        return 'calendar_year', [(date(year, 1, 1), date(year, 12, 31))]
    match = re.fullmatch(r'常年\((春季|夏季|秋季|冬季)(\d{4})适用\)', text)
    if match:
        return 'qualified_season_year', _season_intervals(match[1], [int(match[2])])
    match = re.fullmatch(r'(\d{4})?(春季|夏季|秋季|冬季|淡季\((?:春季|夏季|秋季|冬季)\))', text)
    if match:
        season = match[2][3:-1] if match[2].startswith('淡季(') else match[2]
        years = [int(match[1])] if match[1] else range(start.year, end.year + 1)
        return 'calendar_season', _season_intervals(season, years)
    match = re.fullmatch(r'(\d{4})?(春季|夏季|秋季|冬季)?\(?(\d{1,2})月(\d{1,2})日[-至](\d{1,2})月(\d{1,2})日\)?', text)
    if match:
        # Parentheses must balance; explicit bounds override conventional season bounds.
        if text.count('(') != text.count(')'):
            raise ValueError('unbalanced range')
        first, last = (int(match[3]), int(match[4])), (int(match[5]), int(match[6]))
        date(2000, *first), date(2000, *last)
        years = [int(match[1])] if match[1] else range(max(1, start.year - 1), end.year + 1)
        intervals = [(date(y, *first), date(y + (last < first), *last)) for y in years]
        if match[2]:
            # A named season must intersect the explicit range; summer in January is contradictory.
            for low, high in intervals:
                seasons = _season_intervals(match[2], range(low.year, high.year + 1))
                if not any(max(low, a) <= min(high, b) for a, b in seasons):
                    raise ValueError('season conflicts with explicit range')
        return 'explicit_month_day_range', intervals
    raise ValueError('unknown or ambiguous scope grammar')


def _covers(intervals, start, end):
    """Union containment, including adjacent winter pieces across New Year."""
    cursor = start.toordinal()
    for low, high in sorted(intervals):
        if high.toordinal() < cursor:
            continue
        if low.toordinal() > cursor:
            return False
        cursor = high.toordinal() + 1
        if cursor > end.toordinal():
            return True
    return False


def season_coverage(spec, raw):
    """Return an auditable coverage decision without mutating spec or source scope.

    Existing exact reusable scopes retain their historical authorization semantics.
    All new spellings require a complete, valid itinerary date interval. Chinese
    unbounded seasons use calendar months, never climate or destination inference.
    """
    result = {'original': raw, 'normalized': None, 'covered': False, 'basis': '',
              'provenance': {'version': 1, 'rule': 'unrecognized'},
              'itinerary': {'start': spec.get('departure_date'), 'end': spec.get('end_date')},
              'intervals': []}
    scopes = spec.get('reusable_scopes', [spec.get('season_scope')])
    if isinstance(raw, str) and raw.strip() and raw in scopes:
        result.update(normalized=raw, covered=True, basis='exact explicitly reusable scope')
        result['provenance']['rule'] = 'exact_reusable_scope'
        return result
    try:
        start, end = _date(spec.get('departure_date')), _date(spec.get('end_date'))
        if end < start:
            raise ValueError('itinerary end precedes departure')
        if not isinstance(raw, str):
            raise ValueError('scope must be text')
        text = raw.strip().replace('（', '(').replace('）', ')')
        rule, intervals = _parse_scope(text, start, end)
        result['provenance']['rule'] = rule
        result['intervals'] = [[low.isoformat(), high.isoformat()] for low, high in intervals]
        result['normalized'] = '|'.join('/'.join(bounds) for bounds in result['intervals'])
        result['covered'] = _covers(intervals, start, end)
        result['basis'] = ('entire itinerary covered' if result['covered'] else
                           'scope does not cover entire itinerary')
    except (ValueError, TypeError, OverflowError) as exc:
        result['basis'] = str(exc)
    return result
