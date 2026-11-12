"""Consume only the exact, frozen fixture dispositions authorized for this run.

The authority and source artifacts are pinned independently of ledger contents.
This module does not judge prose, alter fixtures, or change raw preflight results.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / 'research/c6-fixture-disposition-2026-11-03'
TRUSTED_APPROVAL_SOURCE = {
    'path': '/Users/duan/Documents/kimi/Workspaces/旅游agent/docs/'
            'CMD-C6-FIXTURE-DISPOSITION-FOR-CODEX-2026-11-03.md',
    'sha256': '7266e03a67b98aa9c6ed7b20c86cc06fa66d29c14504bf67a5ea32deee0939b2',
}
# Frozen approvals, not destination-specific rules or a reason-wide exemption.
APPROVED_SOURCES = {
    str(EVIDENCE / 'A/accepted-notes.json'): {
        'sha256': '4036a38d87237339c5958387fa2315b1bd55c9a46fde6790ac3593251415fb5d',
        'fixture_sha256': 'e7af4ad40b25550447691d6658243f7a62f39ac04cb5a36a8149e666e7e877c3',
        'collection': 'accepted_notes', 'finding_key': 'raw_finding',
        'disposition': 'accepted_note_by_explicit_CMD',
    },
    str(EVIDENCE / 'B/finding-disposition.json'): {
        'sha256': '8fee691e4dacffa23a1c835feeb85d2647d45fb0f4ab0b1975f4e9f68954e3de',
        'fixture_sha256': 'df0c14b506baaa86240b8cc91564ace9b9f038726f94daee4fe3725e75d9e37b',
        'collection': 'current_findings', 'finding_key': 'finding',
        'disposition': 'declared_per_CMD_not_machine_or_semantic_verified',
    },
}
REASON_DISPOSITIONS = {
    'selection_plan_mapping_unresolved': 'accepted_unprovable_mapping',
    'seed_trip_coverage_unresolved': 'accepted_partial_coverage',
}
ENTRY_FIELDS = {'fixture_sha256', 'finding_fingerprint', 'finding_identity',
                'approval_source', 'disposition', 'source_record'}


class DispositionError(ValueError):
    """An accounting failure that must keep paid dispatch unreachable."""


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DispositionError('duplicate_json_key')
        result[key] = value
    return result


def _invalid_constant(value):
    raise DispositionError('invalid_json_constant')


def _json(blob):
    return json.loads(blob, object_pairs_hook=_object, parse_constant=_invalid_constant)


def _require(condition, code):
    if not condition:
        raise DispositionError(code)


def _bound_json(path, expected):
    blob = Path(path).read_bytes()
    _require(hashlib.sha256(blob).hexdigest() == expected, 'bound_file_hash_mismatch')
    return _json(blob)


def finding_identity(finding):
    """Include selection IDs so three questions on one item never collide."""
    evidence = finding.get('evidence', {})
    selection = evidence.get('selection', {})
    return dict(reason=finding.get('reason'), path=finding.get('path'),
        item_id=evidence.get('item_id') or evidence.get('item', {}).get('id'),
        question_id=selection.get('question_id'), choice_id=selection.get('choice_id'),
        seed_id=evidence.get('seed_id'))


def finding_fingerprint(finding):
    return hashlib.sha256(_canonical(finding_identity(finding)).encode('utf-8')).hexdigest()


def _disposition(finding):
    _require(finding.get('classification') == 'fixture'
             and finding.get('status') == 'unresolved', 'finding_status_not_disposable')
    result = REASON_DISPOSITIONS.get(finding.get('reason'))
    _require(result is not None, 'finding_reason_not_disposable')
    return result


def _authority():
    blob = Path(TRUSTED_APPROVAL_SOURCE['path']).read_bytes()
    _require(hashlib.sha256(blob).hexdigest() == TRUSTED_APPROVAL_SOURCE['sha256'],
             'approval_source_hash_mismatch')


def _source(path, fixture_sha256):
    spec = APPROVED_SOURCES.get(path)
    _require(spec is not None, 'source_record_not_approved')
    _require(spec['fixture_sha256'] == fixture_sha256, 'source_fixture_hash_mismatch')
    document = _bound_json(path, spec['sha256'])
    return spec, document[spec['collection']]


def normalize_dispositions(fixture_path, source_path):
    """Normalize an approved artifact without inventing any new disposition."""
    _authority()
    fixture_sha256 = hashlib.sha256(Path(fixture_path).read_bytes()).hexdigest()
    source_path = str(Path(source_path).resolve())
    spec, records = _source(source_path, fixture_sha256)
    entries = []
    for index, record in enumerate(records):
        finding = record[spec['finding_key']]
        _require(record['disposition'] == spec['disposition'], 'source_disposition_mismatch')
        entries.append(dict(fixture_sha256=fixture_sha256,
            finding_fingerprint=finding_fingerprint(finding), finding_identity=finding_identity(finding),
            approval_source=dict(TRUSTED_APPROVAL_SOURCE), disposition=_disposition(finding),
            source_record=dict(path=source_path, sha256=spec['sha256'], index=index)))
    return dict(version=1, entries=entries)


def _validate_entry(entry, fixture_sha256, findings, sources):
    _require(isinstance(entry, dict) and set(entry) == ENTRY_FIELDS, 'invalid_disposition_entry')
    _require(entry['fixture_sha256'] == fixture_sha256, 'disposition_fixture_hash_mismatch')
    _require(entry['approval_source'] == TRUSTED_APPROVAL_SOURCE, 'approval_source_not_trusted')
    fingerprint = entry['finding_fingerprint']
    _require(isinstance(fingerprint, str) and fingerprint in findings, 'finding_not_present')
    finding_index, finding = findings[fingerprint]
    _require(_canonical(entry['finding_identity']) == _canonical(finding_identity(finding)),
             'finding_identity_mismatch')
    _require(entry['disposition'] == _disposition(finding), 'disposition_category_mismatch')
    source = entry['source_record']
    _require(isinstance(source, dict) and set(source) == {'path', 'sha256', 'index'},
             'invalid_source_record')
    _require(isinstance(source['path'], str), 'invalid_source_record_path')
    if source['path'] not in sources:
        sources[source['path']] = _source(source['path'], fixture_sha256)
    spec, records = sources[source['path']]
    _require(source['sha256'] == spec['sha256'], 'source_record_hash_mismatch')
    index = source['index']
    _require(type(index) is int and 0 <= index < len(records), 'source_record_index_invalid')
    record = records[index]
    _require(record['disposition'] == spec['disposition'], 'source_disposition_mismatch')
    _require(_canonical(record[spec['finding_key']]) == _canonical(finding),
             'source_finding_mismatch')
    return dict(finding_index=finding_index, finding_fingerprint=fingerprint,
        finding_identity=entry['finding_identity'], status='disposed',
        disposition=entry['disposition'], approval_source=entry['approval_source'], source_record=source)


def _consume(fixture, raw_report, binding, receipt):
    _require(isinstance(binding, dict) and set(binding) == {
        'fixture_path', 'expected_sha256', 'disposition_path'}, 'invalid_fixture_binding')
    _require(all(isinstance(value, str) and value for value in binding.values()),
             'invalid_fixture_binding_value')
    fixture_sha256 = binding['expected_sha256']
    actual = _bound_json(binding['fixture_path'], fixture_sha256)
    _require(_canonical(actual) == _canonical(fixture), 'in_memory_fixture_mismatch')
    receipt.update(fixture_path=binding['fixture_path'], fixture_sha256=fixture_sha256,
                   disposition_path=binding['disposition_path'])
    blob = Path(binding['disposition_path']).read_bytes()
    receipt['disposition_sha256'] = hashlib.sha256(blob).hexdigest()
    ledger = _json(blob)
    _require(isinstance(ledger, dict) and set(ledger) == {'version', 'entries'}
             and type(ledger['version']) is int and ledger['version'] == 1
             and isinstance(ledger['entries'], list), 'invalid_disposition_ledger')
    _authority()
    findings = {}
    for index, finding in enumerate(raw_report['findings']):
        fingerprint = finding_fingerprint(finding)
        _require(fingerprint not in findings, 'duplicate_raw_finding_identity')
        findings[fingerprint] = (index, finding)
    seen, sources = set(), {}
    for index, entry in enumerate(ledger['entries']):
        try:
            row = _validate_entry(entry, fixture_sha256, findings, sources)
            fingerprint = row['finding_fingerprint']
            _require(fingerprint not in seen, 'duplicate_disposition_entry')
            seen.add(fingerprint)
            receipt['disposed'].append(row)
        except (DispositionError, KeyError, TypeError, ValueError, OSError) as error:
            receipt['errors'].append(dict(entry_index=index, code=_error_code(error)))
    for fingerprint in findings:
        if fingerprint in seen:
            continue
        receipt['errors'].append(dict(finding_index=findings[fingerprint][0],
            finding_fingerprint=fingerprint, code='finding_not_disposed'))
    receipt['admitted'] = not receipt['errors'] and (raw_report['passed'] is True or bool(findings))


def _error_code(error):
    return str(error) if isinstance(error, DispositionError) else 'disposition_artifact_invalid'


def consume_dispositions(fixture, raw_report, binding):
    """Return a separate admission receipt; leave every raw finding unchanged."""
    receipt = dict(admitted=False, disposed=[], errors=[], raw_preflight_passed=raw_report['passed'])
    try:
        _consume(fixture, raw_report, binding, receipt)
    except (DispositionError, KeyError, TypeError, ValueError, OSError) as error:
        receipt['errors'].append(dict(code=_error_code(error)))
    return receipt
