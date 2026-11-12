"""Structural completion accounting; no destination or prose quality judgments."""
from datetime import date
import re

from .single_session_detail import extract


def validate_completion(markdown, intake):
    """Require supplied trip dates and nonempty structured days before completion.

    Undated legacy prose remains supported. A dated intake requires structured
    days so coverage can be proven without interpreting narrative.
    """
    prose, detail, _ = extract(markdown)
    start, end = intake.get('start_date'), intake.get('end_date')
    if detail is None:
        if start or end:
            raise ValueError('$.days [date_coverage] dated intake requires structured days')
        return
    title = re.search(r'^#[ \t]+([^\r\n]+)', prose, re.M)
    if not title or not title[1].strip():
        raise ValueError('$.markdown.title [nonempty_text] document title required')
    days = detail['days']
    for i, day in enumerate(days):
        for key in ('title', 'summary'):
            if not isinstance(day.get(key), str) or not day[key].strip():
                raise ValueError(f'$.days[{i}].{key} [nonempty_text] daily {key} required')
        if not day.get('stops') and not day.get('timeline'):
            raise ValueError(f'$.days[{i}] [nonempty_day_activity] stops and timeline are both empty')
    if not (start or end):
        return
    try:
        first = date.fromisoformat(start) if start else None
        last = date.fromisoformat(end) if end else None
    except (TypeError, ValueError) as exc:
        raise ValueError('$.intake.start_date/end_date [iso_date_range] valid date range required') from exc
    actual = []
    for i, day in enumerate(days):
        try:
            actual.append(date.fromisoformat(day.get('date')))
        except (TypeError, ValueError) as exc:
            raise ValueError(f'$.days[{i}].date [iso_date] date required') from exc
    if first is None or last is None:
        # Partial intake is valid; only the supplied boundary can be checked.
        boundary = first or last
        if (len(set(actual)) != len(actual) or boundary not in actual
                or (first is not None and min(actual) != first)
                or (last is not None and max(actual) != last)):
            raise ValueError('$.days [date_coverage] dates must match supplied intake boundary')
        return
    expected = (last - first).days + 1
    if (expected <= 0 or len(actual) != expected or len(set(actual)) != expected
            or any(value < first or value > last for value in actual)):
        raise ValueError(f'$.days [date_coverage] expected one day per date {start}..{end}; '
                         f'got {[day.get("date") for day in days]}')
