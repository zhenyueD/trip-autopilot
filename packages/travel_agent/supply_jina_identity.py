"""Reading-layer publisher assessment; originals and supporting reads stay separate."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from .comparison_card_budget import private_json
from .material_reader import _validate_url


def _identity_leads(captures):
    """Observed organisational links are leads only, never identity decisions."""
    seen = {capture['url'] for capture in captures}
    for capture in captures:
        origin = urlsplit(capture['url']).netloc
        for link in capture.get('observed_links', []):
            if not isinstance(link, list) or len(link) != 2:
                continue
            label, url = link
            if not isinstance(label, str) or not isinstance(url, str):
                continue
            try:
                parsed = _validate_url(url)
            except (ValueError, TypeError, UnicodeError):
                continue
            if parsed.netloc != origin or url in seen:
                continue
            if 'about' not in parsed.path.casefold() and label.casefold().strip() not in {'about', 'about us', '关于我们'}:
                continue
            seen.add(url)
            yield {'url': url, 'observed_from': capture['id'], 'label': label}


def _apply_identity_assessments(captures, supplied, assessment, directory):
    by_id = {capture['id']: capture for capture in supplied}
    result, bindings = copy.deepcopy(captures), []
    for capture in result:
        if capture.get('status') == 'read':
            capture['source_provenance'] = {'source_type': 'unknown'}
            capture['source_type'] = 'unknown'
    assessment_hash = hashlib.sha256((directory / 'identity-assessment.json').read_bytes()).hexdigest()
    for row in assessment['assessments']:
        primary = by_id.get(row['source_id'])
        if primary is None or primary['role'] != 'primary' or row['source_type'] == 'unknown':
            continue
        proofs = [quote for quote in row['quotes'] if quote['capture_id'] in by_id
                  and by_id[quote['capture_id']]['role'] == 'primary'
                  and by_id[quote['capture_id']]['url'] == primary['url']
                  and quote['quote'] in by_id[quote['capture_id']]['text']]
        if not proofs:
            continue
        proof = next((quote for quote in proofs if quote['capture_id'] == row['source_id']), proofs[0])
        original = by_id[proof['capture_id']]
        index = int(original['id'].split('-')[-1]) - 1
        result[index]['source_type'] = row['source_type']
        result[index]['source_provenance'] = {
            'source_type': row['source_type'],
            'source_type_evidence': {
                'quote': proof['quote'], 'capture_id': original['id'],
                'assessment_source_id': row['source_id'],
                'basis': 'reader semantic publisher assessment with exact original quotations; uncorroborated',
                'content_sha256': hashlib.sha256(' '.join(original['text'].split()).encode()).hexdigest(),
                'source_content_sha256': hashlib.sha256(original['text'].encode()).hexdigest(),
                'assessment_path': str(directory / 'identity-assessment.json'),
                'assessment_sha256': assessment_hash,
                'quoted_captures': [{**quote,
                    'text_sha256': hashlib.sha256(by_id[quote['capture_id']]['text'].encode()).hexdigest()}
                    for quote in row['quotes']],
            }}
        bindings.append({'assessment_source_id': row['source_id'], 'proof_capture_id': original['id'],
                         'url': original['url'], 'source_type': row['source_type']})
    private_json(directory / 'identity-provenance.json', {'bindings': bindings,
        'unknown_capture_ids': [capture['id'] for capture in supplied if capture['role'] == 'primary'
            and result[int(capture['id'].split('-')[-1]) - 1]['source_type'] == 'unknown']})
    return result


def assess_publisher_captures(reader, captures, directory, request_id, receipt):
    """One batch assessment, with at most two observed supporting-page reads.

    The original capture files are never overwritten. This supplies a reader
    assessment to the existing source gate; claim/date/critic checks stay intact.
    """
    if reader.identity_assessor is None:
        return captures
    primary = []
    for index, capture in enumerate(captures):
        if capture.get('status') == 'read':
            primary.append({**copy.deepcopy(capture), 'id': f'capture-{index + 1}', 'role': 'primary'})
    if not primary:
        return captures
    supports = []
    for index, lead in enumerate(_identity_leads(primary)):
        if index >= 2:
            break
        path = directory / f'identity-support-{index + 1}.json'
        receipt['tool_call_count'] += 1
        private_json(directory / 'receipt.json', receipt)
        capture, _ = reader._read(lead['url'], path)
        receipt.setdefault('identity_support_reads', []).append({**lead, 'path': str(path),
            'status': capture['status'], 'body_length': len(capture.get('text', '')),
            'elapsed_ms': capture.get('elapsed_ms'), 'reason': capture.get('reason')})
        if capture.get('status') == 'read':
            supports.append({**copy.deepcopy(capture), 'id': f'identity-support-{index + 1}', 'role': 'supporting'})
    supplied = [{key: capture[key] for key in ('id', 'role', 'url', 'title', 'text', 'observed_links')
                 if key in capture} for capture in primary + supports]
    private_json(directory / 'identity-input.json', supplied)
    receipt['identity_assessment'] = 'requested'
    private_json(directory / 'receipt.json', receipt)
    budget = getattr(reader.identity_assessor, 'budget', None)
    calls_before = budget.snapshot()['used'] if budget is not None else None
    try:
        assessment = reader.identity_assessor.assess(supplied, request_id=request_id + '-identity')
    finally:
        receipt['product_call_count'] = (budget.snapshot()['used'] - calls_before
                                         if budget is not None else None)
        private_json(directory / 'receipt.json', receipt)
    private_json(directory / 'identity-assessment.json', assessment)
    receipt['identity_assessment'] = 'received'
    return _apply_identity_assessments(captures, primary + supports, assessment, directory)


def assess_original_sources(reader, collected, directory, request_id):
    """Unify Jina and rendered originals before either can acquire official status."""
    from .supply_layered_sources import rendered_source
    sources = collected.get('sources', [])
    if not sources:
        return collected
    directory.mkdir(parents=True, exist_ok=True)
    ledger_by_id = {row.get('source_id'): row for row in collected['ledger']}
    captures = []
    originals = []
    for index, source in enumerate(sources):
        row = ledger_by_id[source['id']]
        path = Path(source.get('capture_path') or row.get('capture_path') or '')
        if not path.resolve().is_relative_to(reader.directory.resolve()) or not path.is_file():
            raise ValueError('Publisher assessment requires the bound original capture file')
        raw = path.read_bytes()
        capture = json.loads(raw)
        if (capture.get('status') != 'read' or capture.get('text') != source['content']
                or capture.get('url') != source['url']):
            raise ValueError('Publisher capture does not match the collected source')
        originals.append({'source_id': source['id'], 'path': str(path),
                          'sha256': hashlib.sha256(raw).hexdigest()})
        capture['observed_links'] = capture.get('observed_links',
            [['', link] for link in capture.get('visible_links', []) if isinstance(link, str)])
        captures.append(capture)
    private_json(directory / 'originals.json', originals)
    receipt = {'product_call_count': 0, 'tool_call_count': 0, 'tool_cost': 'unknown'}
    assessed = assess_publisher_captures(reader, captures, directory, request_id, receipt)
    result = copy.deepcopy(collected)
    replaced, retained = {}, {}
    for index, (old, capture) in enumerate(zip(sources, assessed)):
        path = directory / f'assessed-capture-{index + 1}.json'
        private_json(path, capture)
        qualified = rendered_source(capture)
        if qualified['source'] is None or qualified['source']['id'] != old['id']:
            raise ValueError('Publisher assessment changed the original source identity')
        source, row = qualified['source'], qualified['ledger']
        for key in ('source_layer', 'source_origin', 'transport_provenance', 'content_scope'):
            if key in old:
                source[key] = copy.deepcopy(old[key])
                row[key] = copy.deepcopy(old[key])
        source['capture_path'] = row['capture_path'] = str(path)
        source['original_capture'] = row['original_capture'] = originals[index]
        # Identical URL/text captures have the same downstream ID. Preserve the
        # assessed copy when a later unassessed duplicate would otherwise win.
        previous = retained.get(old['id'])
        if previous is None or source['source_type'] in {'official', 'operator'}:
            retained[old['id']] = source
            replaced[old['id']] = row
    result['sources'] = list(retained.values())
    result['ledger'] = [replaced.get(row.get('source_id'), row) for row in result['ledger']]
    result['publisher_assessment_receipt'] = receipt
    private_json(directory / 'receipt.json', receipt)
    private_json(directory / 'assessed-source-ledger.json', result['ledger'])
    return result
