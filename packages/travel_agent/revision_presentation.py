"""Program-owned current-revision display facts, prepared before critic review.

Literal sentence deduplication makes no semantic inference. Paraphrase repair
belongs to the existing bounded writer/critic loop, not a fuzzy text heuristic.
"""
import copy
import re

from .delivery_truth import revision_diff, _summary


def _dedupe_sentences(value):
    # A period can delimit names, abbreviations or decimals. Leave that
    # ambiguous segmentation to the semantic critic instead of deleting text.
    pieces = re.split(r'(?<=[。！？!?])|(\n+)', value)
    previous, result = None, []
    for piece in pieces:
        if piece is None:
            continue
        key = piece.strip()
        if not key and '\n' in piece:
            previous = None
        if key and key == previous:
            continue
        if key:
            previous = key
        result.append(piece)
    return ''.join(result).strip()


def _dedupe_changed(before, after, field):
    value = after.get(field)
    if isinstance(value, str) and value != before.get(field):
        after[field] = _dedupe_sentences(value)


def presentation_input(previous, raw):
    """Remove generated history before live-reference validation of new content."""
    candidate = copy.deepcopy(raw)
    candidate.pop('change_summary', None)
    candidate.pop('revision_changes', None)
    _dedupe_changed(previous, candidate, 'summary')
    if isinstance(candidate.get('signature'), dict):
        _dedupe_changed(previous.get('signature') or {}, candidate['signature'], 'why')
    old_days = {day['day']: day for day in previous.get('days', [])}
    days = candidate.get('days')
    if isinstance(days, list):
        for day in days:
            if isinstance(day, dict):
                _dedupe_changed(old_days.get(day.get('day'), {}), day, 'rationale')
    return candidate


def _fields(before, after):
    old, new = before or {}, after or {}
    return sorted(key for key in set(old) | set(new)
                  if key not in old or key not in new or old[key] != new[key])


def _change(before, after):
    return {'kind': 'added' if before is None else 'removed' if after is None else 'modified',
            'before': copy.deepcopy(before), 'after': copy.deepcopy(after),
            'fields': _fields(before, after)}


def _item_changes(previous, candidate):
    def indexed(plan):
        return {item['id']: (day['day'], position, item) for day in plan.get('days', [])
                for position, item in enumerate(day.get('items', []), 1)}
    old, new = indexed(previous), indexed(candidate)
    changes = []
    for item_id in sorted(set(old) | set(new)):
        old_day, old_position, before = old.get(item_id, (None, None, None))
        new_day, new_position, after = new.get(item_id, (None, None, None))
        if (old_day, old_position, before) == (new_day, new_position, after):
            continue
        row = {'day': new_day if after is not None else old_day, 'item_id': item_id,
               **_change(before, after)}
        if before is not None and after is not None and old_day != new_day:
            row.update(before_day=old_day, after_day=new_day)
            row['fields'] = sorted(set(row['fields']) | {'day'})
        if before is not None and after is not None and old_position != new_position:
            row.update(before_position=old_position, after_position=new_position)
            row['fields'] = sorted(set(row['fields']) | {'position'})
        changes.append(row)
    return sorted(changes, key=lambda row: (row['day'], row['item_id']))


def _day_changes(previous, candidate):
    def indexed(plan):
        return {day['day']: {key: value for key, value in day.items() if key != 'items'}
                for day in plan.get('days', [])}
    old, new = indexed(previous), indexed(candidate)
    return [{'day': number, **_change(old.get(number), new.get(number))}
            for number in sorted(set(old) | set(new)) if old.get(number) != new.get(number)]


def attach_revision_presentation(previous, validated):
    """Derive display facts only; the reviewed candidate is committed unchanged."""
    candidate = copy.deepcopy(validated)
    labels = _summary(revision_diff(previous, candidate, complete=True))
    # Retain every label while honoring the established twelve-entry contract.
    candidate['change_summary'] = labels if len(labels) <= 12 else labels[:11] + [' '.join(labels[11:])]
    candidate['revision_changes'] = {'version': 1, 'items': _item_changes(previous, candidate),
                                     'days': _day_changes(previous, candidate)}
    return candidate
