"""Extract only explicitly named legacy prose media targets, without new facts."""
import re

from .place_media import _name

_HEADING = re.compile(r'^(#{1,6})\s+(.+)')
_LABEL = re.compile(r'^\s*(?:[-*]\s+)?(?:看什么|怎么走|停多久|何时最好|什么时候最好|坑在哪)[：:]')


def _visible_lines(prose):
    """Fenced examples are not itinerary content; preserve line positions."""
    lines, fence = [], None
    for line in prose.splitlines():
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            lines.append('')
        else:
            lines.append('' if fence else line)
    return lines


def _day_content(lines, start, level):
    end = start + 1
    while end < len(lines):
        heading = _HEADING.match(lines[end])
        if heading and len(heading[1]) <= level:
            break
        end += 1
    return lines[start + 1:end]


def _attractions(lines):
    spots = []
    for index, line in enumerate(lines):
        heading = _HEADING.match(line)
        if not heading:
            continue
        name = _name(heading[2].replace('**', '').strip())
        end = index + 1
        while end < len(lines) and not _HEADING.match(lines[end]):
            end += 1
        if name and any(_LABEL.match(item.replace('**', '')) for item in lines[index + 1:end]):
            spots.append({'name': name})
    return spots


def extract_legacy_media(prose):
    """Return day/name media scaffolding or None; never use narrative as a place.

    Day headings use the renderer's recognition rule. Route stops require the
    renderer's explicit time/action/place table; no named-entity extraction,
    coordinates, travel durations, or destination inference is performed.
    """
    from .single_session_document import _DAY, _day_events

    if not isinstance(prose, str):
        return None
    lines, days = _visible_lines(prose), []
    for index, line in enumerate(lines):
        if not _DAY.match(line.replace('**', '')):
            continue
        heading = _HEADING.match(line)
        if not heading:
            continue
        content = _day_content(lines, index, len(heading[1]))
        # Blank non-table rows reset table header state inside _day_events.
        table_lines = [row if row.strip().startswith('|') else '' for row in content]
        places = [place for _, _, place in _day_events(table_lines)
                  if place != '地点见事项' and _name(place)]
        ordinal = len(days) + 1
        days.append({'id': f'legacy-day-{ordinal}',
                     'title': heading[2].replace('**', '').strip(),
                     'stops': [{'id': f'legacy-day-{ordinal}-stop-{i}', 'name': name}
                               for i, name in enumerate(places, 1)],
                     'attractions': _attractions(content)})
    return {'days': days} if days else None
