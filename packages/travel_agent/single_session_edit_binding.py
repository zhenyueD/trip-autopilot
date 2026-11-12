"""Literal pre-commit edit accounting; no semantic evidence adjudication."""
from collections import Counter
from copy import deepcopy
from decimal import Decimal
import difflib
import re

PRICE = re.compile(r'\{\{price:(E[1-9]\d*)\}\}')
MARKER = re.compile(r'\{\{(?:price|evidence):(E[1-9]\d*)\}\}')
ID = re.compile(r'(?<![A-Za-z0-9_])E[1-9]\d*(?![A-Za-z0-9_])')
REASON = re.compile(r'\[price-replacement (E[1-9]\d*)->(E[1-9]\d*): ([^\]\n]+)\]')
QUANTITY = re.compile(r'(?:[×xX*]|共|数量[：:]?)\s*(\d+(?:\.\d+)?)\s*(份|人|车)')
FIELDS = ('changes', 'judgment', 'alternatives', 'next_action')


def _texts(receipt):
    for field in FIELDS:
        value = receipt.get(field, '')
        yield from value if isinstance(value, list) else [value]


def _occurrences(text):
    return [dict(evidence_id=m[1], line=n, column=m.start() + 1, text=line)
            for n, line in enumerate(text.splitlines(), 1) for m in PRICE.finditer(line)]


def is_complete_line_reorder(before, after):
    """Only self-contained unchanged price lines can move without rebinding."""
    return (before != after and Counter(before.splitlines()) == Counter(after.splitlines())
            and not re.search(r'(?m)^\s*#', before)
            and all(not PRICE.search(line) or re.search(r'[A-Za-z\u4e00-\u9fff]', PRICE.sub('', line))
                    for line in before.splitlines())
            and PRICE.sub('{{price}}', before) != PRICE.sub('{{price}}', after))


def _context_bindings(markdown):
    heading, bindings = (), {}
    for line in markdown.splitlines():
        title = re.match(r'^(#{1,6})\s+(.+)', line)
        if title:
            level = len(title[1])
            heading = tuple(item for item in heading if item[0] < level) + ((level, line),)
        key = (heading, PRICE.sub('{{price}}', line))
        bindings.setdefault(key, []).extend(PRICE.findall(line))
    return bindings


def _replacements(before, after):
    old, new = _occurrences(before), _occurrences(after)
    if is_complete_line_reorder(before, after):
        return []
    pairs = set()
    # If literal context was renamed, diff alignment may treat swapped markers
    # as unchanged moving anchors. Without the complete-line reorder proof
    # above, changed marker order must still account for each positional pair.
    old_sequence = [row['evidence_id'] for row in old]
    new_sequence = [row['evidence_id'] for row in new]
    if len(old_sequence) == len(new_sequence) and old_sequence != new_sequence:
        pairs.update((a, b) for a, b in zip(old_sequence, new_sequence) if a != b)
    left_context, right_context = _context_bindings(before), _context_bindings(after)
    if len(old_sequence) != len(new_sequence):
        # Cancel only bindings with unchanged literal context AND evidence ID.
        # Shared IDs left on both sides can be moved/rebound while additions or
        # deletions hide the swap. There is no unique accounting map to accept.
        old_bound = Counter((key, eid) for key, ids in left_context.items() for eid in ids)
        new_bound = Counter((key, eid) for key, ids in right_context.items() for eid in ids)
        unmatched_old = {eid for (_, eid) in old_bound - new_bound}
        unmatched_new = {eid for (_, eid) in new_bound - old_bound}
        if unmatched_old & unmatched_new:
            raise ValueError('ambiguous price replacement; preserve literal bindings or split revisions')
    for key in left_context.keys() & right_context.keys():
        left, right = left_context[key], right_context[key]
        if len(left) == len(right):
            pairs.update((a, b) for a, b in zip(left, right) if a != b)
    if PRICE.sub('{{price}}', before) == PRICE.sub('{{price}}', after):
        pairs.update((a['evidence_id'], b['evidence_id']) for a, b in zip(old, new)
                     if a['evidence_id'] != b['evidence_id'])
    matcher = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines(), autojunk=False)
    for tag, i, j, k, end in matcher.get_opcodes():
        if tag != 'replace':
            continue
        left = [r['evidence_id'] for r in old if i < r['line'] <= j]
        right = [r['evidence_id'] for r in new if k < r['line'] <= end]
        if not left or not right:
            continue
        if len(left) == len(right):
            pairs.update((a, b) for a, b in zip(left, right) if a != b)
        elif len(set(left)) == len(set(right)) == 1:
            if left[0] != right[0]:
                pairs.add((left[0], right[0]))
        elif set(left) - set(right) and set(right) - set(left):
            raise ValueError('ambiguous price replacement; split into explicit revisions')
    # Separate delete/add hunks must not bypass replacement accounting.
    removed = Counter(r['evidence_id'] for r in old) - Counter(r['evidence_id'] for r in new)
    added = Counter(r['evidence_id'] for r in new) - Counter(r['evidence_id'] for r in old)
    removed = {eid for eid in removed if not any(a == eid for a, _ in pairs)}
    added = {eid for eid in added if not any(b == eid for _, b in pairs)}
    if removed and added:
        if len(removed) != 1 or len(added) != 1:
            raise ValueError('ambiguous price replacement; split into explicit revisions')
        pairs.add((next(iter(removed)), next(iter(added))))
    return [dict(old_evidence_id=a, new_evidence_id=b,
                 old_locations=[r for r in old if r['evidence_id'] == a],
                 new_locations=[r for r in new if r['evidence_id'] == b])
            for a, b in sorted(pairs)]


def _quantity_candidates(line):
    markers = list(PRICE.finditer(line))
    eligible = [m for m in markers if not re.match(r'\s*[/／](?:份|人|车)', line[m.end():])]
    return eligible or markers


def _check_quantities(markdown, evidence):
    # Remove paired Markdown emphasis before recognizing explicit counts.
    clean = markdown.replace('**', '').replace('__', '')
    clean = re.sub(r'([*_])([^\n]*?)\1', r'\2', clean)
    lines = clean.splitlines()
    for index, line in enumerate(lines):
        for quantity in QUANTITY.finditer(line):
            candidates = _quantity_candidates(line)
            if candidates:
                distances = [min(abs(quantity.end() - m.start()),
                                 abs(m.end() - quantity.start())) for m in candidates]
                candidates = [m for m, distance in zip(candidates, distances)
                              if distance == min(distances)]
            else:
                candidates = [m for neighbor in lines[max(0, index - 1):index] + lines[index + 1:index + 2]
                              for m in _quantity_candidates(neighbor)]
            if not candidates:
                continue
            counts = {Decimal(evidence[m[1]]['quantity']) for m in candidates if m[1] in evidence}
            if len(counts) > 1:
                raise ValueError('ambiguous price quantity label; keep its bound marker on the same line')
            if counts and Decimal(quantity[1]) not in counts:
                ids = ', '.join(m[1] for m in candidates)
                raise ValueError(f'price quantity mismatch: {ids} label {quantity[1]} != ledger quantity')


def validate(before, after, receipt, evidence, *, restored_version=None):
    """Return harness-owned audit fields, or reject before any version writes.

    Reasons use an existing receipt text field, e.g.
    [price-replacement E31->E19: 用户改为一份套餐]
    A verified undo supplies its exact stored target internally, never via receipt.
    """
    result = deepcopy(receipt)
    added_ids = {m[1] for line in difflib.unified_diff(before.splitlines(), after.splitlines())
                 if line.startswith('+') and not line.startswith('+++')
                 for m in MARKER.finditer(line[1:])}
    reasons = {}
    for text in _texts(receipt):
        for match in REASON.finditer(text):
            key = (match[1], match[2])
            if not match[3].strip() or key in reasons:
                raise ValueError('price replacement reason must be nonempty and unique per pair')
            reasons[key] = match[3].strip()
        claims = REASON.sub(lambda match: match[3], text)
        missing = set(ID.findall(claims)) - added_ids
        if missing:
            raise ValueError('receipt evidence ID absent from added diff markers: ' + ', '.join(sorted(missing)))
    replacements = _replacements(before, after)
    for row in replacements:
        pair = (row['old_evidence_id'], row['new_evidence_id'])
        reason = reasons.pop(pair, None)
        if restored_version is not None:
            reason = f'恢复已校验版本 {restored_version}'
        if reason is None:
            raise ValueError('price replacement requires reason in receipt changes/judgment: '
                             f'[price-replacement {pair[0]}->{pair[1]}: 具体替换理由]')
        if pair[1] not in added_ids:
            raise ValueError('replacement new evidence ID absent from added diff markers')
        row['reason'] = reason
    if reasons:
        raise ValueError('price replacement reason does not match actual replacement')
    _check_quantities(after, evidence)
    replaced = {r['old_evidence_id'] for r in replacements}
    old_ids, new_ids = set(PRICE.findall(before)), set(PRICE.findall(after))
    totals = {(evidence[eid].get('currency'), evidence[eid].get('total_amount'))
              for eid in new_ids if eid in evidence}
    if restored_version is None:
        for eid in old_ids - new_ids - replaced:
            record = evidence.get(eid, {})
            if (record.get('total_amount') is None
                    or (record.get('currency'), record['total_amount']) not in totals):
                raise ValueError(f'unique calculated total removed: {eid}; retain a bound total marker')
    result['price_replacements'] = replacements
    result['edit_binding_checks'] = dict(status='PASS', added_marker_ids=sorted(added_ids),
                                         restored_version=restored_version)
    return result
