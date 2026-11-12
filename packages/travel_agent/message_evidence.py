"""Read-only views of the same normalized claims used by revision consumers."""
from copy import deepcopy
import re

from .detail_reviews import apply_review, source_withdrawn, usable_claim
from .detail_synthesis import _claims, _source_references
from .entry_trust import is_direct_read
from .material_details import _link_states

MAX_LEDGER_SOURCES = 8


def _overlaps_ledger(citation, source, ledger):
    """Route only same-origin text spans through their canonical disposition."""
    quote = citation.get('quote')
    if (not source or not isinstance(quote, str) or not quote.strip()
            or quote not in source['content']):
        return False  # Let the normal citation validator reject malformed input.
    identities = {source.get(key) for key in ('id', 'material_id', 'source_id')
                  if isinstance(source.get(key), str)}
    identities.update(source.get('_origin_material_ids', ()))
    origin = source.get('_origin_content', source['content'])
    for detail in ledger.values():
        canonical = detail['content']
        if detail.get('material_id') not in identities or not canonical:
            continue
        start = origin.find(canonical)
        while start >= 0:
            # A quote may cross the canonical span's boundary. Searching this
            # window covers every literal quote occurrence that overlaps it.
            if origin.find(quote, max(0, start - len(quote) + 1),
                           start + len(canonical) + len(quote) - 1) >= 0:
                return True
            start = origin.find(canonical, start + 1)
    return False


def ledger_disposition(raw, source_index):
    """Validate complete semantic disposition, then select canonical quotes.

    Relation and brief basis are model assessments; neither can change the
    source's object, applicability, evidence status, or recorded quantity.
    """
    ledger = {id_: source for id_, source in source_index.items() if source.get('detail_id')}
    if not ledger:
        return raw, None
    reviews = raw.get('evidence_review')
    if not isinstance(reviews, list):
        return None, 'missing_evidence_review'
    dispositions = {}
    for row in reviews:
        if (not isinstance(row, dict) or not isinstance(row.get('source_id'), str)
                or row['source_id'] not in ledger or row['source_id'] in dispositions
                or row.get('relation') not in ('direct', 'related', 'unrelated')
                or not isinstance(row.get('reason'), str) or not row['reason'].strip()
                or len(row['reason']) > 240):
            return None, 'invalid_evidence_review'
        dispositions[row['source_id']] = {key: row[key] for key in ('source_id', 'relation', 'reason')}
    if set(dispositions) != set(ledger):
        return None, 'incomplete_evidence_review'
    citations = raw.get('citations', [])
    if (not isinstance(citations, list) or len(citations) > 8
            or any(not isinstance(row, dict) or not isinstance(row.get('source_id'), str)
                   for row in citations)):
        return None, 'invalid_citation'
    kept = [row for row in citations if row['source_id'] not in ledger
            and not _overlaps_ledger(row, source_index.get(row['source_id']), ledger)]
    selected = [id_ for id_, row in dispositions.items() if row['relation'] != 'unrelated']
    kept += [{'source_id': id_, 'quote': ledger[id_]['content']} for id_ in selected]
    result = {**raw, 'citations': kept, 'evidence_review': list(dispositions.values())}
    if (selected and not any(dispositions[id_]['relation'] == 'direct' for id_ in selected)) or not kept:
        result['status'] = 'unknown'
    return result, None


def material_rows(trip):
    return [row for row in [*(trip.get('material_sources') or []),
            *(trip.get('source_evidence') or []),
            *((trip.get('plan') or {}).get('source_evidence') or [])] if isinstance(row, dict)]


def material_links(rows):
    ledger = {'materials': [{'material_id': row.get('id'), 'details': [], 'source': {
        'source_url': row.get('url') or row.get('source_url'),
        'direct_read': is_direct_read(row)}} for row in rows]}
    _link_states(ledger, rows)
    return {row['material_id']: row['source'] for row in ledger['materials']
            if isinstance(row['material_id'], str)}


def _has_detail_reference(value):
    if isinstance(value, list):
        return any(_has_detail_reference(row) for row in value)
    if not isinstance(value, dict):
        return False
    return any(value.get(key) for key in ('detail_ids', 'guide_step_ids')) or any(
        _has_detail_reference(child) for child in value.values())


def _ledger_expected(trip):
    # These are persisted extraction receipts/references, not an inference from
    # uploaded text. Legacy/new trips may have materials but no extraction yet.
    metrics = trip.get('metrics')
    extracted = isinstance(metrics, dict) and any(
        key in metrics for key in ('detail_creation', 'detail_synthesis'))
    return bool(trip.get('detail_research_context') or extracted
                or _has_detail_reference(trip.get('plan')))


def _readable_material(material):
    if (not isinstance(material, dict) or not isinstance(material.get('material_id'), str)
            or not isinstance(material.get('details'), list)
            or not isinstance(material.get('guide_steps', []), list)
            or (material.get('source') is not None and not isinstance(material['source'], dict))):
        return False
    for detail in material['details']:
        if (not isinstance(detail, dict) or any(not isinstance(detail.get(key), str)
                or not detail[key].strip() for key in ('id', 'field', 'value', 'quote'))):
            return False
        for key in ('entity', 'conditions'):
            value = detail.get(key)
            if value is not None and (not isinstance(value, dict) or any(
                    not isinstance(value.get(part), str) for part in ('value', 'quote'))):
                return False
    return all(isinstance(step, dict) for step in material.get('guide_steps', []))


def ledger_access(trip):
    """Distinguish absent extraction from an unreadable persisted ledger.

    An empty, readable ledger says nothing about semantic relevance. Failed
    extraction/source-link rows also remain readable evidence of their limits.
    """
    if 'material_detail_ledger' not in trip:
        return {'status': 'unreachable', 'reason': 'missing_expected_ledger'} if _ledger_expected(trip) else {
            'status': 'not_expected', 'reason': 'no_persisted_extraction'}
    ledger = trip['material_detail_ledger']
    if ledger == {} and not _ledger_expected(trip):
        return {'status': 'reachable'}
    if (not isinstance(ledger, dict) or not isinstance(ledger.get('materials'), list)
            or any(not _readable_material(row) for row in ledger['materials'])):
        return {'status': 'unreachable', 'reason': 'malformed_ledger'}
    return {'status': 'reachable'}


def ledger_sources(trip):
    ledger = apply_review(trip.get('material_detail_ledger') or {})
    ledger.setdefault('materials', [])
    # Reuse revision normalization's aliases/redirect-chain failure propagation.
    # A previously unavailable source cannot be revived by a later missing receipt.
    unavailable = {row.get('material_id') for row in ledger.get('materials', [])
                   if (row.get('source') or {}).get('link_available') is False}
    _link_states(ledger, material_rows(trip))
    for material in ledger.get('materials', []):
        if material.get('material_id') in unavailable and material.get('source'):
            material['source'].update(link_available=False, direct_read=False)
    claims, sources, restricted_materials, restricted_ids = _claims(ledger), [], set(), set()
    for material in ledger.get('materials', []):
        details = material.get('details') or []
        if any(not usable_claim(row) for row in details):
            # Raw text would otherwise bypass a field-level semantic veto.
            restricted_materials.add(material.get('material_id'))
        restricted_ids.update(row.get('id') for row in
            [*details, *(material.get('guide_steps') or [])] if not usable_claim(row))
        if source_withdrawn(material):
            restricted_materials.add(material.get('material_id'))
            continue
        for claim in details:
            identifier = claim.get('id')
            if identifier not in claims or not isinstance(claim.get('quote'), str):
                continue
            reference = _source_references([identifier], ledger, claims)[0]
            sources.append({'id': 'ledger:' + identifier,
                'label': '材料字段 · ' + str(claim.get('field') or ''),
                'content': claim['quote'], 'status': 'source_stated_unverified',
                **{key: deepcopy(reference.get(key)) for key in (
                    'url', 'source_url', 'direct_read', 'link_available', 'indirect',
                    'indirectness', 'source_type', 'language', 'checked_at', 'applicability')},
                **{key: deepcopy(claim.get(key)) for key in ('field', 'value', 'entity', 'conditions')},
                'detail_id': identifier, 'material_id': material.get('material_id'),
                'untrusted_external_content': True})
    return sources, restricted_materials, restricted_ids


def restricted_plan_paths(plan, restricted_ids, restricted_source_ids=()):
    """Quarantine the nearest semantic owner, retaining unrelated siblings."""
    blocked = set()
    def walk(value, path='', owner=None, collection=''):
        if isinstance(value, dict):
            if collection in ('items', 'actions', 'decisions', 'preparation'):
                owner = path
            bad_claim = any(identifier in restricted_ids for key in ('detail_ids', 'guide_step_ids')
                   for identifier in (value[key] if isinstance(value.get(key), list) else [])
                   if isinstance(identifier, str))
            bad_source = any(identifier in restricted_source_ids
                for key in ('source_ids', 'material_ids', 'evidence_ids')
                for identifier in (value[key] if isinstance(value.get(key), list) else [])
                if isinstance(identifier, str))
            bad_source = bad_source or any(value.get(key) in restricted_source_ids
                for key in ('source_id', 'material_id', 'evidence_id') if isinstance(value.get(key), str))
            if bad_claim or bad_source:
                blocked.add(owner if owner is not None else path)
            for key, child in value.items():
                token = str(key).replace('~', '~0').replace('/', '~1')
                walk(child, path + '/' + token, owner, key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + '/' + str(index), owner, collection)
    walk(plan)
    return blocked


_EN_DAYS = dict(zip(('one two three four five six seven eight nine ten eleven twelve '
    'thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty').split(), range(1, 21)))
_EN_DAYS.update(dict(zip(('first second third fourth fifth sixth seventh eighth ninth tenth '
    'eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth '
    'nineteenth twentieth').split(), range(1, 21))))
_DAY_WORDS = '|'.join(_EN_DAYS)
_DAY_REFERENCE = re.compile(
    r'第\s*([〇零一二三四五六七八九十两\d]+)\s*[天日]|'
    r'(\d+)\s*(?:日目|일차)|'
    rf'\b(?:day|jour|día|dia|tag)\s+(\d+|{_DAY_WORDS})\b|'
    rf'\b(\d+(?:st|nd|rd|th)?|{_DAY_WORDS})\s+day\b|'
    r'اليوم\s+(\d+)', re.I)


def _day_number(word):
    word = word.casefold()
    if word in _EN_DAYS:
        return _EN_DAYS[word]
    word = re.sub(r'(st|nd|rd|th)$', '', word)
    if word.isdecimal():
        return int(word)
    digits = {char: index for index, char in enumerate('零一二三四五六七八九')}
    digits.update({'〇': 0, '两': 2})
    if word in digits:
        return digits[word]
    if word.count('十') == 1:
        tens, ones = word.split('十')
        if (not tens or tens in digits) and (not ones or ones in digits):
            return digits.get(tens, 1) * 10 + digits.get(ones, 0)
    return None


def _without_day_anchors(prose, source_index):
    days = {row['day'] for row in source_index.values()
            if row.get('status') == 'current_plan' and type(row.get('day')) is int}
    days.update(int(row['content']) for row in source_index.values()
        if row.get('status') == 'current_plan' and re.fullmatch(r'/days/\d+/day', row.get('path', ''))
        and isinstance(row.get('content'), str) and row['content'].isdecimal())
    valid = True
    def replace(match):
        nonlocal valid
        if _day_number(next(word for word in match.groups() if word is not None)) not in days:
            valid = False
        return ' <existing plan day> '
    result = _DAY_REFERENCE.sub(replace, prose)
    return result if valid else None


def judgments(raw, source_index=None):
    rows = raw.get('judgments', [])
    if not isinstance(rows, list) or len(rows) > 3:
        return None
    result = []
    for row in rows:
        if (not isinstance(row, dict) or row.get('label') not in ('建议', '经验判断')
                or any(not isinstance(row.get(key), str) or not row[key].strip()
                       or len(row[key]) > 700 for key in ('text', 'reason'))):
            return None
        prose = _without_day_anchors(row['text'] + ' ' + row['reason'], source_index or {})
        if prose is None:
            return None
        # Specific quantities, times, prices, phone numbers and URLs belong to
        # the cited fact layer, never to an uncited experience estimate.
        if re.search(r'\d|(?:https?://|javascript:|data:|\]\s*\()|'
                     r'[〇零一二三四五六七八九十百千万两]+\s*'
                     r'(?:元|块|迪拉姆|美元|分钟|小时|点|天|公里|米|岁|AED|USD|EUR|CNY|RMB|GBP|JPY|SAR|HKD|dollars?|dirhams?)|'
                     r'\b(?:one|two|three|four|five|six|seven|eight|nine|ten|hundred|thousand)\s+'
                     r'(?:dollars?|dirhams?|aed|hours?|minutes?|days?|kilometers?)\b', prose, re.I):
            return None
        result.append({key: row[key].strip() for key in ('label', 'text', 'reason')})
    return result


def cited_fact_text(citations):
    """The model selects relevant evidence; it cannot rewrite its quantities."""
    if any(citation.get('detail_id') for citation in citations):
        return ledger_fact_text(citations)
    lines = []
    labels = {'time': '时间安排', 'title': '安排', 'transport': '交通', 'price': '价格记录'}
    for citation in citations:
        if citation.get('status') == 'current_plan':
            day = citation.get('day')
            label = f'当前行程第 {day} 天' if type(day) is int else '当前行程'
            label += ' · ' + labels.get(citation.get('path', '').rsplit('/', 1)[-1], '记录')
        else:
            label = citation.get('label') or '已读取材料'
        lines.append(label + '：“' + citation['quote'] + '”')
    return '\n'.join(lines)


def ledger_fact_text(citations):
    """Keep numerical ledger answers attached to the recorded object/conditions.

    The model may offer separate judgments, but cannot relabel a quoted taxi
    fare as a charter quotation merely by attaching a syntactically valid quote.
    """
    lines = ['已找到材料中的记录；以下是来源陈述，当前适用性和实际报价仍未核实：']
    for citation in citations:
        line = '“' + citation['quote'] + '”'
        for key, label in (('entity', '对象'), ('conditions', '条件')):
            value = citation.get(key)
            if isinstance(value, dict) and isinstance(value.get('value'), str):
                line += '；' + label + '：' + value['value']
        if citation.get('indirect') or citation.get('indirectness'):
            line += '；间接来源'
        if citation.get('link_available') is False:
            line += '；原链接当前不可用'
        lines.append(line)
    lines.append('原文对应的对象、计价方式和条件不能替换为当前行程的服务报价。')
    return '\n'.join(lines)
